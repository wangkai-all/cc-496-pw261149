"""Scoring runs, alert lifecycle and revision jobs.

All mutations that can move risk funnel through here so every change leaves a
``scoring_run`` and (when it opens/closes alerts) ``warning_event`` rows behind.
"""

from __future__ import annotations

import json
import time

from .catalog import TIERS
from .scoring import (
    ALERT_TIERS,
    aliases_of,
    current_feature_set,
    json_evidence,
    json_missing,
    score_entity,
)
from .store import parse_ts, utcnow

TRIGGER_LABELS = {
    "initial": "初始全量",
    "features": "特征修订",
    "merge": "客户合并",
    "label": "标签回溯",
    "manual": "手动",
    "undo": "撤销",
}


def _open_warning(conn, entity_id: int, tier: str, run_id: int, now: str, score: float) -> dict | None:
    existing = conn.execute(
        "SELECT * FROM warning WHERE customer_id=? AND closed_at IS NULL", (entity_id,)
    ).fetchone()
    key = f"cust:{entity_id}"
    if existing:
        conn.execute(
            """UPDATE warning SET tier=?, last_seen_at=?, last_run_id=?,
                   occurrences=occurrences+1, score_delta=? WHERE id=?""",
            (tier, now, run_id, score - existing["opening_score"], existing["id"]),
        )
        conn.execute(
            "INSERT INTO warning_event(warning_id,at,kind,detail,run_id,tier) VALUES(?,?,?,?,?,?)",
            (existing["id"], now, "deduped", f"同一客户仍处于 {tier}，合并进已有告警（第 {existing['occurrences']+1} 次命中）", run_id, tier),
        )
        return {"warning_id": existing["id"], "action": "deduped"}
    # reopen chain: last closed warning for this customer
    last_closed = conn.execute(
        "SELECT * FROM warning WHERE customer_id=? AND closed_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (entity_id,),
    ).fetchone()
    cur = conn.execute(
        """INSERT INTO warning(customer_id,tier,dedupe_key,first_seen_at,last_seen_at,opened_run_id,
               last_run_id,score_delta,opening_score,supersedes)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (entity_id, tier, key, now, now, run_id, run_id, 0.0, score, last_closed["id"] if last_closed else None),
    )
    wid = cur.lastrowid
    conn.execute(
        "INSERT INTO warning_event(warning_id,at,kind,detail,run_id,tier) VALUES(?,?,?,?,?,?)",
        (wid, now, "opened", f"进入 {tier} 层，风险分 {score:.1f}", run_id, tier),
    )
    if last_closed:
        conn.execute(
            "INSERT INTO warning_event(warning_id,at,kind,detail,run_id,tier) VALUES(?,?,?,?,?,?)",
            (wid, now, "reopened", f"关闭后再次触发，接续告警 #{last_closed['id']}（关闭原因：{last_closed['close_reason']}）", run_id, tier),
        )
    return {"warning_id": wid, "action": "opened"}


def _close_warning(conn, wid: int, now: str, reason: str, by: str, run_id: int | None, detail: str) -> None:
    conn.execute(
        "UPDATE warning SET closed_at=?, close_reason=?, closed_by=?, last_run_id=COALESCE(?,last_run_id) WHERE id=?",
        (now, reason, by, run_id, wid),
    )
    conn.execute(
        "INSERT INTO warning_event(warning_id,at,kind,detail,run_id,reason) VALUES(?,?,?,?,?,?)",
        (wid, now, "closed", detail, run_id, reason),
    )


def warning_sync(conn, entity_id: int, tier: str, run_id: int, now: str, score: float) -> dict:
    """Open/refresh/close alerts for one customer after a score is written.

    A run replayed against an *older* cut-off must not rewrite alert state that
    a later run already produced: if this run's wall-clock time precedes the
    alert's last_seen_at, the alert is left untouched (its score is still
    recorded in customer_score).
    """
    out = {"opened": 0, "deduped": 0, "closed": 0, "closed_ids": []}
    existing = conn.execute(
        "SELECT * FROM warning WHERE customer_id=? AND closed_at IS NULL", (entity_id,)
    ).fetchone()
    if existing and now < existing["last_seen_at"]:
        out["stale_skipped"] = 1
        return out
    # A run replayed before alert activity that a later run already recorded is
    # an append-only scoring replay: it must not reopen/close alerts either.
    latest_event = conn.execute(
        "SELECT MAX(at) AS at FROM warning_event WHERE warning_id IN "
        "(SELECT id FROM warning WHERE customer_id=?)", (entity_id,)
    ).fetchone()["at"]
    if latest_event and now < latest_event:
        out["stale_skipped"] = 1
        return out
    if tier in ALERT_TIERS:
        res = _open_warning(conn, entity_id, tier, run_id, now, score)
        out[res["action"]] += 1
        out["warning_id"] = res["warning_id"]
        if existing and existing["tier"] != tier:
            conn.execute(
                "INSERT INTO warning_event(warning_id,at,kind,detail,run_id,tier) VALUES(?,?,?,?,?,?)",
                (existing["id"], now, "refreshed", f"告警等级 {existing['tier']} → {tier}", run_id, tier),
            )
    elif existing:
        _close_warning(
            conn, existing["id"], now, "recovered", "system", run_id,
            f"风险降至 {tier}（{score:.1f}分），自动恢复关闭",
        )
        out["closed"] += 1
        out["closed_ids"].append(existing["id"])
    return out


def tier_transition(conn, entity_id: int, as_of: str) -> str | None:
    """Tier from the most recent earlier run whose cut-off is <= this as_of."""
    row = conn.execute(
        """SELECT cs.tier FROM customer_score cs
           JOIN scoring_run r ON r.id = cs.run_id
           WHERE cs.customer_id=? AND r.as_of<=?
           ORDER BY r.as_of DESC, r.id DESC LIMIT 1""",
        (entity_id, as_of),
    ).fetchone()
    return row["tier"] if row else None


def run_scoring(
    conn,
    *,
    as_of: str | None = None,
    trigger: str = "manual",
    trigger_ref: str | None = None,
    customer_ids: list[int] | None = None,
    scope: str | None = None,
    run_at: str | None = None,
    feature_set_version: int | None = None,
    notes: str | None = None,
) -> dict:
    started = time.perf_counter()
    now = run_at or utcnow()
    as_of = as_of or now
    set_version = feature_set_version or current_feature_set(conn)
    # Which customers are separate entities is a function of the cut-off, not of
    # today's merge flags: a run replayed at an older as_of must score the ids
    # that had not yet been merged *then*.
    all_ids = []
    for r in conn.execute("SELECT id FROM customer ORDER BY id"):
        survivor, _ids, _chain = aliases_of(conn, r["id"], as_of)
        if survivor == r["id"] and r["id"] not in all_ids:
            all_ids.append(r["id"])
    if customer_ids is None:
        targets = all_ids
        scope = scope or "full"
        skipped = 0
    else:
        # resolve requested ids to surviving entities at as_of
        resolved = []
        skipped = 0
        for cid in customer_ids:
            ent, _ids, _chain = aliases_of(conn, cid, as_of)
            if ent != cid:
                skipped += 1
            if ent in all_ids and ent not in resolved:
                resolved.append(ent)
            elif ent not in all_ids:
                skipped += 1
        targets = resolved
        scope = scope or "affected"
    cur = conn.execute(
        """INSERT INTO scoring_run(run_at,as_of,feature_set_version,trigger,trigger_ref,scope,
               scored_count,skipped_count,duration_ms,watermark,notes)
           VALUES(?,?,?,?,?,?,0,0,0,?,?)""",
        (now, as_of, set_version, trigger, trigger_ref, scope,
         conn.execute("SELECT MAX(available_at) AS m FROM feature_version WHERE available_at<=?", (as_of,)).fetchone()["m"],
         notes),
    )
    run_id = cur.lastrowid
    changed: list[dict] = []
    alerts = {"opened": 0, "deduped": 0, "closed": 0}
    for eid in targets:
        res = score_entity(conn, eid, as_of, set_version)
        prev_tier = tier_transition(conn, eid, as_of)
        changed_flag = prev_tier is not None and prev_tier != res.tier
        conn.execute(
            """INSERT INTO customer_score(run_id,customer_id,entity_id,score,tier,prev_tier,
                   tier_changed,evidence,missing,stale_features,late_features,feature_set_version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, eid, eid, res.score, res.tier, prev_tier, 1 if changed_flag else 0,
                json_evidence(res.evidence), json_missing(res.missing),
                res.stale_features, res.late_features, set_version,
            ),
        )
        w = warning_sync(conn, eid, res.tier, run_id, now, res.score)
        alerts["opened"] += w["opened"]
        alerts["deduped"] += w["deduped"]
        alerts["closed"] += w["closed"]
        if changed_flag:
            changed.append({"customer_id": eid, "from": prev_tier, "to": res.tier})
    duration = int((time.perf_counter() - started) * 1000)
    conn.execute(
        "UPDATE scoring_run SET scored_count=?, skipped_count=?, duration_ms=? WHERE id=?",
        (len(targets), skipped, duration, run_id),
    )
    conn.commit()
    return {
        "run_id": run_id,
        "trigger": trigger,
        "scope": scope,
        "as_of": as_of,
        "run_at": now,
        "scored": len(targets),
        "skipped": skipped,
        "changed": changed,
        "alerts": alerts,
        "duration_ms": duration,
    }


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def revise_feature(
    conn,
    *,
    customer_id: int,
    code: str,
    value: float | None,
    missing_reason: str | None = None,
    effective_at: str,
    available_at: str | None = None,
    source: str = "manual_revision",
    note: str | None = None,
    as_of: str | None = None,
    run_at: str | None = None,
) -> dict:
    """Append a feature revision, supersede the previous active one, rescore.

    Returns the affected customer list plus the job record. Revisions that
    arrive after ``as_of`` are stored but do not affect the run (they show as
    late) — the caller can then run again with a later cut-off.
    """
    now = run_at or utcnow()
    available_at = available_at or now
    as_of = as_of or available_at
    old = conn.execute(
        """SELECT * FROM feature_version WHERE customer_id=? AND feature_code=? AND status='active'
           ORDER BY effective_at DESC, id DESC LIMIT 1""",
        (customer_id, code),
    ).fetchone()
    # Only revisions at or before the new business time are replaced. A
    # backdated correction must not hide a revision with newer effective_at
    # that is still valid data.
    conn.execute(
        """UPDATE feature_version SET status='superseded'
           WHERE customer_id=? AND feature_code=? AND status='active' AND effective_at<=?""",
        (customer_id, code, effective_at),
    )
    cur = conn.execute(
        """INSERT INTO feature_version(customer_id,feature_code,value,effective_at,available_at,
               status,missing_reason,source,note,created_at,replaces_id)
           VALUES(?,?,?,?,?, 'active', ?,?,?,?,?)""",
        (customer_id, code, value, effective_at, available_at, missing_reason, source, note, now,
         old["id"] if old else None),
    )
    new_id = cur.lastrowid
    conn.commit()
    return finish_job(
        conn,
        kind="backfill",
        customer_ids=[customer_id],
        as_of=as_of,
        trigger="features",
        trigger_ref=f"feature_version:{new_id}",
        note=note or f"{code} 修订",
        run_at=now,
        extra={"revision_id": new_id, "replaced_id": old["id"] if old else None,
               "late": available_at > as_of,
               "available_at": available_at, "effective_at": effective_at},
    )


def replay_label(
    conn,
    *,
    customer_id: int,
    value: str,
    effective_at: str,
    available_at: str | None = None,
    backdated: bool | None = None,
    retract: bool = False,
    source: str = "csm_replay",
    note: str | None = None,
    as_of: str | None = None,
    run_at: str | None = None,
) -> dict:
    """Append a new label version; with ``retract`` also withdraw the prior one.

    A retraction is itself a versioned event: old versions are never rewritten,
    the superseded label gets ``retracted=1`` and the new row explains why.
    """
    now = run_at or utcnow()
    available_at = available_at or now
    as_of = as_of or available_at
    last = conn.execute(
        "SELECT * FROM label_event WHERE customer_id=? ORDER BY label_version DESC, id DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    version = (last["label_version"] + 1) if last else 1
    if backdated is None:
        backdated = parse_ts(effective_at) < parse_ts(available_at)
    cur = conn.execute(
        """INSERT INTO label_event(customer_id,label_version,value,effective_at,available_at,
               backdated,retracted,retracts,source,note,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (customer_id, version, value, effective_at, available_at, 1 if backdated else 0,
         0, None, source, note, now),
    )
    label_id = cur.lastrowid
    # A retraction withdraws the label it supersedes (usually the previous
    # version), not itself: the new row records the withdrawal event, and the
    # old row's `retracted` flag is itself time-stamped by the new row so a
    # historical replay does not see the withdrawal before it happened.
    retracted_id = None
    if retract and last:
        conn.execute("UPDATE label_event SET retracted=1 WHERE id=?", (last["id"],))
        conn.execute("UPDATE label_event SET retracts=? WHERE id=?", (last["id"], label_id))
        retracted_id = last["id"]
    resolved = apply_label_to_feedback(conn, customer_id, value, effective_at, label_id,
                                       retract, at=available_at,
                                       retracted_label_id=retracted_id)
    conn.commit()
    job = finish_job(
        conn,
        kind="label_replay",
        customer_ids=[customer_id],
        as_of=as_of,
        trigger="label",
        trigger_ref=f"label:{label_id}",
        note=note or f"标签 v{version} {value}{'（回溯）' if backdated else ''}",
        run_at=now,
        extra={
            "label_id": label_id,
            "version": version,
            "backdated": bool(backdated),
            "retracted": bool(retract),
            "effective_at": effective_at,
            "available_at": available_at,
            "feedback_updated": resolved,
        },
    )
    return job


def apply_label_to_feedback(conn, customer_id: int, value: str, effective_at: str,
                            label_id: int, retract: bool, at: str | None = None,
                            retracted_label_id: int | None = None) -> list[dict]:
    """Attach a label outcome to prior feedback and re-open overturned dismissals.

    ``at`` is when the label became known; it timestamps the audit event so the
    timeline stays chronological even when history is replayed out of order.
    """
    entity, _ids, _chain = aliases_of(conn, customer_id, effective_at)
    rows = conn.execute(
        """SELECT f.*, w.customer_id AS w_customer, w.closed_at, w.close_reason, w.tier
           FROM feedback f JOIN warning w ON w.id=f.warning_id
           WHERE (f.customer_id=? OR w.customer_id=?) AND f.outcome IS NULL""",
        (customer_id, entity),
    ).fetchall()
    updated = []
    if retract:
        # Retracting a label withdraws the outcome but keeps the record: every
        # feedback row that this label had resolved is annotated, never deleted.
        if retracted_label_id is not None:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE outcome_label_id=?", (retracted_label_id,)
            ).fetchall()
        for f in rows:
            conn.execute(
                """UPDATE feedback SET outcome_retracted=1, outcome_at=NULL,
                       outcome_note=? WHERE id=?""",
                (f"标签于 {effective_at} 撤回，此结论不再成立", f["id"]),
            )
            updated.append({"feedback_id": f["id"], "warning_id": f["warning_id"],
                            "outcome": "retracted", "retracted_label_id": retracted_label_id})
            # If that label had overturned a dismissal, the retraction puts the
            # alert back where it was: closed as retracted, not open on a claim
            # nobody stands behind.
            marker = f"标签（业务时间 {effective_at[:10]}）"
            reopened = conn.execute(
                """SELECT * FROM warning WHERE id=? AND closed_at IS NULL
                   AND EXISTS (SELECT 1 FROM warning_event e
                               WHERE e.warning_id=warning.id AND e.kind='reopened'
                                 AND e.detail LIKE ?)""",
                (f["warning_id"], marker + "%"),
            ).fetchone()
            if f["verdict"] == "invalid" and reopened:
                _close_warning(conn, reopened["id"], at or utcnow(), "retracted", "system", None,
                               f"标签（业务时间 {effective_at[:10]}）已撤销，恢复此前的误报关闭")
                updated[-1]["warning_closed"] = reopened["id"]
        return updated
    outcome = value if value in ("churned", "retained") else None
    if not outcome:
        return updated
    for f in rows:
        note = "标签回溯确认" if value == "churned" else "标签回溯否定"
        conn.execute(
            """UPDATE feedback SET outcome=?, outcome_at=?, outcome_label_id=?, outcome_note=?
               WHERE id=?""",
            (outcome, effective_at, label_id, note, f["id"]),
        )
        updated.append({"feedback_id": f["id"], "warning_id": f["warning_id"], "outcome": outcome})
        # A dismissal ("invalid") overturned by a churned label: reopen the alert.
        if f["verdict"] == "invalid" and outcome == "churned" and f["close_reason"] == "dismissed":
            seg = conn.execute(
                "SELECT * FROM warning WHERE id=? AND customer_id=?", (f["warning_id"], f["w_customer"])
            ).fetchone()
            already_open = conn.execute(
                "SELECT id FROM warning WHERE customer_id=? AND closed_at IS NULL",
                (f["w_customer"],),
            ).fetchone()
            if seg and not already_open:
                stamp = at or utcnow()
                row = conn.execute(
                    """SELECT id FROM scoring_run WHERE run_at<=?
                       ORDER BY run_at DESC, id DESC LIMIT 1""",
                    (stamp,),
                ).fetchone()
                last_run = row["id"] if row else None
                conn.execute(
                    "UPDATE warning SET closed_at=NULL, close_reason=NULL, closed_by=NULL WHERE id=?",
                    (seg["id"],),
                )
                conn.execute(
                    "INSERT INTO warning_event(warning_id,at,kind,detail,run_id,tier) VALUES(?,?,?,?,?,?)",
                    (seg["id"], at or utcnow(), "reopened",
                     f"标签（业务时间 {effective_at[:10]}）确认流失，推翻此前的无效反馈", last_run, seg["tier"]),
                )
            elif seg and already_open:
                conn.execute(
                    "INSERT INTO warning_event(warning_id,at,kind,detail,run_id,tier) VALUES(?,?,?,?,?,?)",
                    (already_open["id"], at or utcnow(), "refreshed",
                     f"标签（业务时间 {effective_at[:10]}）确认流失：此前的误报关闭(#%d)被推翻" % seg["id"],
                     None, seg["tier"]),
                )
                updated[-1]["reopen_note"] = "已有打开中的告警；在打开告警上标注了推翻记录"
    return updated


def merge_customers(
    conn,
    *,
    from_id: int,
    to_id: int,
    effective_at: str | None = None,
    available_at: str | None = None,
    reason: str = "人工合并",
    as_of: str | None = None,
    run_at: str | None = None,
) -> dict:
    now = run_at or utcnow()
    effective_at = effective_at or now
    available_at = available_at or now
    as_of = as_of or available_at
    if from_id == to_id:
        raise ValueError("不能合并到自身")
    ent_from, _ids, _chain = aliases_of(conn, from_id, available_at)
    ent_to, _ids2, _chain2 = aliases_of(conn, to_id, available_at)
    if ent_from == ent_to:
        raise ValueError("两个客户已属于同一实体")
    cur = conn.execute(
        """INSERT INTO customer_merge(from_customer,to_customer,effective_at,available_at,reason,created_at)
           VALUES(?,?,?,?,?,?)""",
        (from_id, to_id, effective_at, available_at, reason, now),
    )
    merge_id = cur.lastrowid
    conn.execute(
        "UPDATE customer SET merged_into=?, merged_at=? WHERE id=?",
        (to_id, effective_at, from_id),
    )
    # Close open alerts on the retired id; they are superseded by the survivor's.
    open_w = conn.execute(
        "SELECT * FROM warning WHERE customer_id=? AND closed_at IS NULL", (from_id,)
    ).fetchall()
    for w in open_w:
        _close_warning(conn, w["id"], now, "merged", "system", None,
                       f"客户并入 #{to_id}，告警移交合并后实体")
    conn.commit()
    job = finish_job(
        conn,
        kind="merge",
        customer_ids=[from_id, to_id],
        as_of=as_of,
        trigger="merge",
        trigger_ref=f"merge:{merge_id}",
        note=reason,
        run_at=now,
        extra={"merge_id": merge_id, "from": from_id, "to": to_id, "closed_warnings": [w["id"] for w in open_w]},
    )
    return job


def undo_merge(conn, *, merge_id: int, as_of: str | None = None, run_at: str | None = None) -> dict:
    now = run_at or utcnow()
    row = conn.execute("SELECT * FROM customer_merge WHERE id=?", (merge_id,)).fetchone()
    if not row:
        raise ValueError("合并记录不存在")
    if row["undone_at"]:
        raise ValueError("该合并已撤销")
    conn.execute("UPDATE customer_merge SET undone_at=? WHERE id=?", (now, merge_id))
    conn.execute("UPDATE customer SET merged_into=NULL, merged_at=NULL WHERE id=?", (row["from_customer"],))
    conn.commit()
    job = finish_job(
        conn,
        kind="merge",
        customer_ids=[row["from_customer"], row["to_customer"]],
        as_of=as_of or now,
        trigger="undo",
        trigger_ref=f"merge:{merge_id}:undo",
        note="撤销合并",
        run_at=now,
        extra={"merge_id": merge_id, "undone": True},
    )
    return job


def finish_job(
    conn,
    *,
    kind: str,
    customer_ids: list[int],
    as_of: str,
    trigger: str,
    trigger_ref: str | None,
    note: str,
    extra: dict,
    run_at: str | None = None,
) -> dict:
    """Record a job, rescore the affected customers and log the audit row."""
    started = run_at or utcnow()
    run = run_scoring(
        conn,
        as_of=as_of,
        trigger=trigger,
        trigger_ref=trigger_ref,
        customer_ids=customer_ids,
        scope="affected",
        run_at=started,
        notes=note,
    )
    finished = started
    result = {"note": note, "run": run, **extra}
    cur = conn.execute(
        """INSERT INTO job_run(kind,started_at,finished_at,affected_json,result_json,note)
           VALUES(?,?,?,?,?,?)""",
        (kind, started, finished, json.dumps(customer_ids), json.dumps(result, ensure_ascii=False), note),
    )
    conn.commit()
    return {"job_id": cur.lastrowid, "kind": kind, "note": note, "run": run, **extra}


# --------------------------------------------------------------------------
# Feedback & alert actions
# --------------------------------------------------------------------------


def add_feedback(conn, *, warning_id: int, verdict: str, note: str | None, user: str, at: str | None = None) -> dict:
    w = conn.execute("SELECT * FROM warning WHERE id=?", (warning_id,)).fetchone()
    if not w:
        raise ValueError("告警不存在")
    if w["closed_at"] and w["close_reason"] not in ("dismissed",):
        raise ValueError("该告警已关闭，不能反馈")
    now = at or utcnow()
    cur = conn.execute(
        """INSERT INTO feedback(warning_id,customer_id,verdict,note,by_user,at)
           VALUES(?,?,?,?,?,?)""",
        (warning_id, w["customer_id"], verdict, note, user, now),
    )
    fb_id = cur.lastrowid
    # If an outcome label is already on record, attach it immediately.
    label = conn.execute(
        """SELECT * FROM label_event
           WHERE customer_id=? AND retracted=0 AND value IN ('churned','retained')
           ORDER BY label_version DESC, id DESC LIMIT 1""",
        (w["customer_id"],),
    ).fetchone()
    if label:
        conn.execute(
            """UPDATE feedback SET outcome=?, outcome_at=?, outcome_label_id=?, outcome_note=?
               WHERE id=?""",
            (label["value"], label["effective_at"], label["id"], "反馈创建时已存在结果标签", fb_id),
        )
    conn.commit()
    return {"feedback_id": fb_id, "warning_id": warning_id, "verdict": verdict,
            "outcome": label["value"] if label else None}


def close_warning(conn, *, warning_id: int, reason: str, user: str, note: str | None = None, at: str | None = None) -> dict:
    w = conn.execute("SELECT * FROM warning WHERE id=?", (warning_id,)).fetchone()
    if not w:
        raise ValueError("告警不存在")
    if w["closed_at"]:
        raise ValueError("该告警已关闭")
    allowed = {"dismissed", "resolved", "duplicate"}
    if reason not in allowed:
        raise ValueError(f"关闭原因必须是 {sorted(allowed)}")
    now = at or utcnow()
    detail = note or {"dismissed": "人工判定为误报", "resolved": "已处理并恢复", "duplicate": "重复告警"}[reason]
    dup_of = None
    if reason == "duplicate":
        # keep the row queryable but link it to the surviving warning
        target = conn.execute(
            "SELECT id FROM warning WHERE customer_id=? AND closed_at IS NULL AND id<>?",
            (w["customer_id"], warning_id),
        ).fetchone()
        dup_of = target["id"] if target else None
        detail += f"；重复于告警 #{dup_of}" if dup_of else "；保留记录以供查询"
    _close_warning(conn, warning_id, now, reason, user, None, detail)
    conn.commit()
    return {"warning_id": warning_id, "closed_at": now, "reason": reason, "duplicate_of": dup_of}


def reopen_warning(conn, *, warning_id: int, user: str, at: str | None = None) -> dict:
    w = conn.execute("SELECT * FROM warning WHERE id=?", (warning_id,)).fetchone()
    if not w:
        raise ValueError("告警不存在")
    if not w["closed_at"]:
        raise ValueError("告警仍处于打开状态")
    open_now = conn.execute(
        "SELECT id FROM warning WHERE customer_id=? AND closed_at IS NULL", (w["customer_id"],)
    ).fetchone()
    if open_now:
        raise ValueError("该客户已有打开中的告警，请在原告警上继续")
    now = at or utcnow()
    conn.execute(
        "UPDATE warning SET closed_at=NULL, close_reason=NULL, closed_by=NULL, last_seen_at=? WHERE id=?",
        (now, warning_id),
    )
    conn.execute(
        "INSERT INTO warning_event(warning_id,at,kind,detail,run_id,tier) VALUES(?,?,?,?,NULL,?)",
        (warning_id, now, "reopened", f"{user} 手动重新打开", w["tier"]),
    )
    conn.commit()
    return {"warning_id": warning_id, "reopened_at": now}


def set_feature_set(conn, *, version: int) -> dict:
    row = conn.execute("SELECT * FROM feature_set WHERE version=?", (version,)).fetchone()
    if not row:
        raise ValueError("特征集版本不存在")
    conn.execute("UPDATE feature_set SET active=0")
    conn.execute("UPDATE feature_set SET active=1 WHERE version=?", (version,))
    conn.commit()
    return {"active_version": version}


def campaign(
    conn,
    *,
    customer_ids: list[int],
    as_of: str | None = None,
    trigger: str = "features",
    trigger_ref: str | None = None,
    note: str = "",
    kind: str = "rescore",
    run_at: str | None = None,
) -> dict:
    """Rescore an explicit affected set (used for batch backfills/merges in bulk)."""
    now = run_at or utcnow()
    return finish_job(
        conn,
        kind=kind,
        customer_ids=customer_ids,
        as_of=as_of or now,
        trigger=trigger,
        trigger_ref=trigger_ref,
        note=note,
        run_at=now,
        extra={"affected": len(customer_ids)},
    )
