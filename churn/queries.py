"""Read models for the API/UI. Everything here is side-effect free."""

from __future__ import annotations

import json

from .catalog import FEATURE_PARAMS, MISSING_REASON_LABELS, TIER_LABELS, TIERS
from .scoring import STALE_DAYS, current_feature_set, score_entity
from .store import add_days, days_between, utcnow

TIER_ORDER = {t: i for i, t in enumerate(TIERS)}


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _one(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def bootstrap(conn) -> dict:
    defs = _rows(conn.execute("SELECT * FROM feature_def ORDER BY code"))
    for d in defs:
        d.update(FEATURE_PARAMS.get(d["code"], {}))
    sets = _rows(conn.execute(
        """SELECT fs.*, (SELECT COUNT(*) FROM feature_set_member m WHERE m.set_version=fs.version) AS members
           FROM feature_set fs ORDER BY version"""))
    for s in sets:
        s["weights"] = {
            r["feature_code"]: r["weight"]
            for r in conn.execute(
                "SELECT feature_code, weight FROM feature_set_member WHERE set_version=?",
                (s["version"],))
        }
    customers = _rows(conn.execute(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM customer_merge m
                    WHERE m.from_customer=c.id AND m.undone_at IS NULL) AS merge_count
           FROM customer c ORDER BY c.id"""))
    # One option per distinct cut-off. Several incremental jobs can share the
    # same as_of; keep the newest run of each so the picker is not noisy.
    as_of_options = []
    seen_as_of: set[str] = set()
    for r in conn.execute(
        """SELECT r.id AS run_id, r.as_of, r.run_at, r.trigger, r.scope, r.scored_count, r.notes
           FROM scoring_run r ORDER BY r.as_of DESC, r.id DESC"""
    ):
        if r["as_of"] in seen_as_of:
            continue
        seen_as_of.add(r["as_of"])
        as_of_options.append(dict(r))
    as_of_options.reverse()
    return {
        "features": defs,
        "feature_sets": sets,
        "customers": customers,
        "as_of_options": as_of_options,
        "latest_as_of": _latest_as_of(conn),
        "tiers": [{"code": t, "label": TIER_LABELS[t]} for t in TIERS],
        "missing_reasons": [{"code": k, "label": v} for k, v in MISSING_REASON_LABELS.items()],
        "stale_days": STALE_DAYS,
        "now": utcnow(),
        "server_version": "churn-watch/1.0",
    }


def _latest_as_of(conn) -> str | None:
    row = conn.execute("SELECT MAX(as_of) AS a FROM scoring_run").fetchone()
    return row["a"]


def as_of_or_latest(conn, as_of: str | None) -> str:
    return as_of or _latest_as_of(conn) or utcnow()


def _entities_at(conn, as_of: str) -> list[int]:
    ids = []
    for r in conn.execute("SELECT id FROM customer ORDER BY id"):
        row = conn.execute(
            """SELECT to_customer FROM customer_merge
               WHERE from_customer=? AND (undone_at IS NULL OR undone_at>?)
                 AND available_at<=?
               ORDER BY available_at DESC, id DESC LIMIT 1""",
            (r["id"], as_of, as_of),
        ).fetchone()
        if not row:
            ids.append(r["id"])
    return ids


def _warnings_at(conn, as_of: str) -> list[dict]:
    """Warnings as they stood at ``as_of``, rebuilt from the append-only log.

    The ``warning`` row only holds *today's* state; a historical cut-off must
    not show a dismissal, merge or recovery that happened afterwards. Each
    event already carries the tier / close reason it established, so replaying
    the events with ``at <= as_of`` reconstructs the state without look-ahead.
    """
    rows = _rows(conn.execute(
        """SELECT w.*, c.name AS customer_name, c.segment, c.region
           FROM warning w JOIN customer c ON c.id=w.customer_id
           WHERE w.first_seen_at<=? ORDER BY w.id""", (as_of,)))
    events: dict[int, list[dict]] = {}
    for e in conn.execute("SELECT * FROM warning_event ORDER BY at, id"):
        if e["at"] <= as_of:
            events.setdefault(e["warning_id"], []).append(dict(e))
    out = []
    for w in rows:
        evs = events.get(w["id"], [])
        # Legacy rows (written before events carried structured fields) fall
        # back to the warning row; new rows are exact.
        tier = w["tier"]
        occurrences = 0
        last_seen = None
        closed_at = None
        close_reason = None
        for e in evs:
            kind = e["kind"]
            if e["tier"]:
                tier = e["tier"]
            if kind == "opened":
                occurrences = 1
                last_seen = e["at"]
                closed_at, close_reason = None, None
            elif kind == "deduped":
                occurrences += 1
                last_seen = e["at"]
            elif kind == "refreshed":
                pass
            elif kind == "closed":
                closed_at = e["at"]
                close_reason = e["reason"] or w["close_reason"]
            elif kind == "reopened":
                closed_at, close_reason = None, None
                last_seen = e["at"]
        if not evs:  # no event log (pre-migration row): fall back to the row
            occurrences = w["occurrences"]
            last_seen = w["last_seen_at"]
            closed_at = w["closed_at"] if w["closed_at"] and w["closed_at"] <= as_of else None
            close_reason = w["close_reason"] if closed_at else None
        out.append({
            **w,
            "tier": tier,
            "occurrences": occurrences,
            "last_seen_at": last_seen or w["first_seen_at"],
            "closed_at": closed_at,
            "close_reason": close_reason if closed_at else None,
            "events": evs,
        })
    return out


def _labels_at(conn, as_of: str) -> list[dict]:
    """Label versions with ``retracted`` projected to the cut-off.

    A withdrawal is itself a dated label version pointing at the one it voids
    (``retracts``); a replay before that row arrived must still treat the old
    label as standing. Future rows are kept (the ledger discloses them with
    ``known_at_as_of``) but policy picks filter on ``available_at``.
    """
    rows = _rows(conn.execute("SELECT * FROM label_event ORDER BY label_version, id"))
    withdrawn_by: dict[int, list[dict]] = {}
    for l in rows:
        if l["retracts"]:
            withdrawn_by.setdefault(l["retracts"], []).append(l)
    out = []
    for l in rows:
        retracted = False
        if l["retracted"]:
            # legacy rows only carry the flag; new rows also say when
            marks = withdrawn_by.get(l["id"])
            retracted = any(m["available_at"] <= as_of for m in marks) if marks else True
        out.append({**l, "retracted": 1 if retracted else 0})
    return out


def _known_outcomes(conn, as_of: str) -> set[int]:
    """Label ids whose availability is at or before the cut-off."""
    return {r["id"] for r in conn.execute(
        "SELECT id FROM label_event WHERE available_at<=?", (as_of,))}


def _project_feedback(conn, as_of: str) -> list[dict]:
    """Feedback as of a cut-off: rows recorded later and outcomes learned later
    are withheld, so a replay cannot cite a verdict nobody had yet."""
    known = _known_outcomes(conn, as_of)
    labels = {l["id"]: l for l in _labels_at(conn, as_of)}
    out = []
    for f in conn.execute("SELECT * FROM feedback WHERE at<=? ORDER BY id", (as_of,)):
        f = dict(f)
        if f["outcome_label_id"] is not None and f["outcome_label_id"] not in known:
            f["outcome"] = None
            f["outcome_at"] = None
            f["outcome_note"] = None
            f["outcome_retracted"] = 0
        elif f["outcome_retracted"]:
            label = labels.get(f["outcome_label_id"])
            if label is not None and not label["retracted"]:
                f["outcome_retracted"] = 0  # withdrawal not known yet
        out.append(f)
    return out


def _latest_scores(conn, as_of: str, entity_ids: list[int] | None = None) -> dict[int, dict]:
    ids = entity_ids if entity_ids is not None else _entities_at(conn, as_of)
    out: dict[int, dict] = {}
    for eid in ids:
        row = conn.execute(
            """SELECT cs.*, r.as_of, r.run_at, r.trigger, r.scope, r.notes AS run_notes
               FROM customer_score cs JOIN scoring_run r ON r.id=cs.run_id
               WHERE cs.customer_id=? AND r.as_of<=?
               ORDER BY r.as_of DESC, r.id DESC LIMIT 1""",
            (eid, as_of),
        ).fetchone()
        if row:
            out[eid] = dict(row)
    return out


def portfolio(conn, as_of: str | None = None, tier: str | None = None,
              segment: str | None = None, region: str | None = None,
              q: str | None = None) -> dict:
    as_of = as_of_or_latest(conn, as_of)
    scores = _latest_scores(conn, as_of)
    customers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM customer")}
    warnings = {}
    for w in _warnings_at(conn, as_of):
        warnings.setdefault(w["customer_id"], []).append(w)
    items = []
    for eid, s in scores.items():
        c = customers[eid]
        open_w = next((w for w in warnings.get(eid, []) if not w["closed_at"]), None)
        last_w = open_w or (max(warnings.get(eid, []), key=lambda w: w["id"]) if warnings.get(eid) else None)
        ev = json.loads(s["evidence"] or "[]")
        miss = json.loads(s["missing"] or "{}")
        top = sorted([e for e in ev if e.get("value") is not None],
                     key=lambda e: abs(e["contribution"]), reverse=True)[:3]
        items.append({
            "customer_id": eid,
            "name": c["name"],
            "region": c["region"],
            "segment": c["segment"],
            "merged_into": c["merged_into"],
            "score": s["score"],
            "tier": s["tier"],
            "prev_tier": s["prev_tier"],
            "tier_changed": bool(s["tier_changed"]),
            "stale_features": s["stale_features"],
            "late_features": s["late_features"],
            "missing_count": len(miss),
            "missing": miss,
            "top_evidence": top,
            "as_of": s["as_of"],
            "run_id": s["run_id"],
            "trigger": s["trigger"],
            "warning": {
                "id": last_w["id"],
                "open": not last_w["closed_at"],
                "tier": last_w["tier"],
                "occurrences": last_w["occurrences"],
                "closed_reason": last_w["close_reason"],
            } if last_w else None,
        })
    if tier:
        items = [i for i in items if i["tier"] == tier]
    if segment:
        items = [i for i in items if i["segment"] == segment]
    if region:
        items = [i for i in items if i["region"] == region]
    if q:
        needle = q.strip().lower()
        items = [i for i in items
                 if needle in i["name"].lower() or needle in str(i["customer_id"])]
    items.sort(key=lambda i: (TIER_ORDER.get(i["tier"], 9), -i["score"]))
    counts = {t: 0 for t in TIERS}
    for i in items:
        counts[i["tier"]] += 1
    open_alerts = sum(1 for i in items if i["warning"] and i["warning"]["open"])
    return {
        "as_of": as_of,
        "counts": counts,
        "total": len(items),
        "open_alerts": open_alerts,
        "late_entities": sum(1 for i in items if i["late_features"]),
        "stale_entities": sum(1 for i in items if i["stale_features"]),
        "missing_entities": sum(1 for i in items if i["missing_count"]),
        "items": items,
    }


def risk_history(conn, as_of: str | None = None) -> dict:
    """Tier counts of the whole book at each distinct cut-off, plus score lines.

    A point is the portfolio as it stood at that ``as_of`` — the latest score per
    entity whose run cut-off is at or before it. Incremental jobs only rescore a
    few customers, so counting rows per run would show a one-customer book.
    """
    as_of = as_of_or_latest(conn, as_of)
    runs = _rows(conn.execute(
        """SELECT r.id, r.as_of, r.run_at, r.trigger, r.scope, r.scored_count
           FROM scoring_run r WHERE r.as_of<=?
           ORDER BY r.as_of ASC, r.id ASC""",
        (as_of,)))
    # one entry per cut-off: the last run recorded at that time names the trigger
    by_as_of: dict[str, dict] = {}
    for r in runs:
        by_as_of[r["as_of"]] = r
    points = []
    for cut, r in sorted(by_as_of.items()):
        counts = {t: 0 for t in TIERS}
        for s in _latest_scores(conn, cut).values():
            counts[s["tier"]] = counts.get(s["tier"], 0) + 1
        points.append({"run_id": r["id"], "as_of": cut, "trigger": r["trigger"],
                       "counts": counts, "scored": sum(counts.values())})
    # per-entity score series (entity id may change after merges; use current entities)
    entity_ids = _entities_at(conn, as_of)
    customers = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM customer")}
    series = []
    latest = _latest_scores(conn, as_of)
    risk_ids = [eid for eid, s in sorted(latest.items(), key=lambda kv: -kv[1]["score"])
                if s["tier"] in ("critical", "high")]
    for eid in risk_ids[:6]:
        pts = _rows(conn.execute(
            """SELECT r.as_of, cs.score, cs.tier, cs.tier_changed, r.trigger
               FROM customer_score cs JOIN scoring_run r ON r.id=cs.run_id
               WHERE cs.customer_id=? AND r.as_of<=?
               ORDER BY r.as_of ASC, r.id ASC""",
            (eid, as_of)))
        # collapse same as_of duplicates (replay of same day): keep last
        collapsed = {}
        for p in pts:
            collapsed[p["as_of"]] = p
        series.append({"customer_id": eid, "name": customers[eid],
                       "points": [collapsed[k] for k in sorted(collapsed)]})
    return {"as_of": as_of, "points": points, "series": series,
            "entities": [{"id": eid, "name": customers[eid]} for eid in entity_ids]}


def customer_detail(conn, customer_id: int, as_of: str | None = None) -> dict:
    as_of = as_of_or_latest(conn, as_of)
    c = _one(conn.execute("SELECT * FROM customer WHERE id=?", (customer_id,)))
    if not c:
        raise KeyError(customer_id)
    # resolve entity at as_of
    from .scoring import aliases_of
    entity, owner_ids, chain = aliases_of(conn, customer_id, as_of)
    c_entity = _one(conn.execute("SELECT * FROM customer WHERE id=?", (entity,)))
    scores = _rows(conn.execute(
        """SELECT cs.*, r.as_of, r.run_at, r.trigger, r.scope, r.feature_set_version AS run_set_version,
                  r.notes
           FROM customer_score cs JOIN scoring_run r ON r.id=cs.run_id
           WHERE cs.customer_id=? AND r.as_of<=?
           ORDER BY r.as_of ASC, r.id ASC""",
        (entity, as_of)))
    for s in scores:
        s["evidence"] = json.loads(s["evidence"] or "[]")
        s["missing"] = json.loads(s["missing"] or "{}")
    latest = scores[-1] if scores else None
    explanation = score_entity(conn, entity, as_of)
    defs = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM feature_def")}
    evidence = []
    for ev in explanation.evidence:
        d = defs[ev["code"]]
        item = {
            "code": ev["code"], "label": d["label"], "unit": d["unit"],
            "direction": d["direction"], "weight": ev["weight"],
            "value": ev["value"], "contribution": ev["contribution"],
            "effective_at": ev.get("effective_at"), "available_at": ev.get("available_at"),
            "stale_days": ev.get("stale_days"), "late": ev.get("late"),
            "late_days": ev.get("late_days"), "owner_id": ev.get("owner_id"),
            "revision_id": ev.get("revision_id"),
        }
        if ev["value"] is None:
            m = explanation.missing.get(ev["code"], {})
            item.update({
                "missing_reason": m.get("reason"),
                "missing_reason_label": m.get("reason_label"),
                "missing_stale_days": m.get("stale_days"),
                "missing_source": m.get("source"),
            })
        evidence.append(item)
    evidence.sort(key=lambda e: abs(e["contribution"] or 0), reverse=True)
    revisions = _rows(conn.execute(
        f"""SELECT fv.*, f.label FROM feature_version fv
            JOIN feature_def f ON f.code=fv.feature_code
            WHERE fv.customer_id IN ({','.join('?'*len(owner_ids))})
            ORDER BY fv.effective_at DESC, fv.id DESC""", owner_ids))
    for r in revisions:
        r["visible_at_as_of"] = r["available_at"] <= as_of
        r["is_late_for_as_of"] = r["available_at"] > as_of
        r["is_current_pick"] = any(e.get("revision_id") == r["id"] for e in explanation.evidence)
    labels = _rows(conn.execute(
        """SELECT * FROM label_event WHERE customer_id IN (%s)
           ORDER BY label_version ASC, id ASC""" % ",".join("?" * len(owner_ids)),
        owner_ids))
    for l in labels:
        l["known_at_as_of"] = l["available_at"] <= as_of
    feedback_at = _project_feedback(conn, as_of)
    owner_set = set(owner_ids)
    warnings = [w for w in _warnings_at(conn, as_of) if w["customer_id"] in owner_set]
    for w in warnings:
        w["feedback_count"] = sum(1 for f in feedback_at if f["warning_id"] == w["id"])
        w["feedback"] = [f for f in feedback_at if f["warning_id"] == w["id"]]
    # Merges known at the cut-off, with undos projected to it as well: a
    # historical view must not offer to undo a merge that had not happened.
    merges = _rows(conn.execute(
        """SELECT * FROM customer_merge WHERE available_at<=?
           ORDER BY id""", (as_of,)))
    for m in merges:
        m["undone"] = bool(m["undone_at"] and m["undone_at"] <= as_of)
        if not m["undone"]:
            m["undone_at"] = None
    return {
        "as_of": as_of,
        "customer": c,
        "entity": entity,
        "entity_customer": c_entity,
        "merge_chain": chain,
        "aliases": [dict(r) for r in conn.execute(
            "SELECT * FROM customer WHERE id IN (%s)" % ",".join("?" * len(owner_ids)), owner_ids)],
        "latest": latest,
        "explanation": {
            "score": explanation.score,
            "tier": explanation.tier,
            "evidence": evidence,
            "missing": explanation.missing,
            "stale_features": explanation.stale_features,
            "late_features": explanation.late_features,
            "feature_set_version": explanation.feature_set_version,
        },
        "history": scores,
        "revisions": revisions,
        "labels": labels,
        "warnings": warnings,
        "merges": merges,
        "customers": _rows(conn.execute("SELECT id,name,segment,region,merged_into FROM customer ORDER BY id")),
        "all_features": _rows(conn.execute("SELECT code,label,unit,direction FROM feature_def")),
        "feature_sets": _rows(conn.execute("SELECT version,name,active,created_at FROM feature_set ORDER BY version")),
    }


def warnings_list(conn, status: str = "open", as_of: str | None = None,
                  customer_id: int | None = None) -> dict:
    as_of = as_of_or_latest(conn, as_of)
    # Alerts are rebuilt from their event log at the cut-off: a historical view
    # must neither reveal a warning that had not opened yet nor show a closure,
    # merge or recovery that happened afterwards.
    items = _warnings_at(conn, as_of)
    if customer_id:
        items = [w for w in items if w["customer_id"] == customer_id]
    feedback_at = _project_feedback(conn, as_of)
    for w in items:
        w["feedback"] = [f for f in feedback_at if f["warning_id"] == w["id"]]
        w["feedback_count"] = len(w["feedback"])
        latest = conn.execute(
            """SELECT score, tier FROM customer_score cs JOIN scoring_run r ON r.id=cs.run_id
               WHERE cs.customer_id=? AND r.as_of<=? ORDER BY r.as_of DESC, r.id DESC LIMIT 1""",
            (w["customer_id"], as_of)).fetchone()
        w["current"] = dict(latest) if latest else None
    counts = {
        "open": sum(1 for w in items if not w["closed_at"]),
        "closed": sum(1 for w in items if w["closed_at"]),
        "dismissed": sum(1 for w in items if w["close_reason"] == "dismissed"),
        "deduped_events": sum(1 for w in items for e in w["events"] if e["kind"] == "deduped"),
    }
    if status == "open":
        items = [w for w in items if not w["closed_at"]]
    elif status == "closed":
        items = [w for w in items if w["closed_at"]]
    # open first, then by severity, then most recently hit (stable multi-pass)
    items.sort(key=lambda w: w["id"], reverse=True)
    items.sort(key=lambda w: w["last_seen_at"] or "", reverse=True)
    items.sort(key=lambda w: (bool(w["closed_at"]), TIER_ORDER.get(w["tier"], 9)))
    return {"as_of": as_of, "status": status, "counts": counts, "items": items}


def warning_detail(conn, warning_id: int) -> dict:
    w = _one(conn.execute(
        """SELECT w.*, c.name AS customer_name FROM warning w JOIN customer c ON c.id=w.customer_id
           WHERE w.id=?""", (warning_id,)))
    if not w:
        raise KeyError(warning_id)
    w["events"] = _rows(conn.execute(
        "SELECT * FROM warning_event WHERE warning_id=? ORDER BY at, id", (warning_id,)))
    w["feedback"] = _rows(conn.execute("SELECT * FROM feedback WHERE warning_id=? ORDER BY id", (warning_id,)))
    w["scores"] = _rows(conn.execute(
        """SELECT cs.score, cs.tier, cs.prev_tier, r.as_of, r.trigger, r.id AS run_id
           FROM customer_score cs JOIN scoring_run r ON r.id=cs.run_id
           WHERE cs.customer_id=? ORDER BY r.as_of, r.id""", (w["customer_id"],)))
    return w


def runs_list(conn, limit: int = 100) -> dict:
    runs = _rows(conn.execute(
        """SELECT r.*, fs.name AS feature_set_name,
                  (SELECT COUNT(*) FROM customer_score cs WHERE cs.run_id=r.id AND cs.tier_changed=1) AS changed_count
           FROM scoring_run r JOIN feature_set fs ON fs.version=r.feature_set_version
           ORDER BY r.as_of DESC, r.id DESC LIMIT ?""", (limit,)))
    for r in runs:
        r["changed"] = _rows(conn.execute(
            """SELECT customer_id, tier, prev_tier, score FROM customer_score
               WHERE run_id=? AND tier_changed=1""", (r["id"],)))
        r["affected_ids"] = [row["customer_id"] for row in conn.execute(
            "SELECT customer_id FROM customer_score WHERE run_id=?", (r["id"],))]
    return {"items": runs}


def jobs_list(conn) -> dict:
    items = _rows(conn.execute("SELECT * FROM job_run ORDER BY id DESC"))
    for j in items:
        j["affected"] = json.loads(j["affected_json"])
        j["result"] = json.loads(j["result_json"])
    return {"items": items}


def version_compare(conn, as_of: str | None = None) -> dict:
    as_of = as_of_or_latest(conn, as_of)
    sets = _rows(conn.execute("SELECT * FROM feature_set ORDER BY version"))
    for s in sets:
        s["weights"] = {r["feature_code"]: r["weight"] for r in conn.execute(
            "SELECT feature_code, weight FROM feature_set_member WHERE set_version=?", (s["version"],))}
    entity_ids = _entities_at(conn, as_of)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM customer")}
    rows_out = []
    for eid in entity_ids:
        per = {}
        for s in sets:
            res = score_entity(conn, eid, as_of, s["version"])
            per[s["version"]] = {"score": res.score, "tier": res.tier,
                                 "evidence": {e["code"]: e["contribution"] for e in res.evidence}}
        versions = [s["version"] for s in sets]
        base = per[versions[0]]
        for v in versions[1:]:
            cur = per[v]
            cur["delta"] = round(cur["score"] - base["score"], 2)
            cur["tier_changed"] = cur["tier"] != base["tier"]
        rows_out.append({"customer_id": eid, "name": names[eid], "versions": per})
    rows_out.sort(key=lambda r: -max(v["score"] for v in r["versions"].values()))
    active = current_feature_set(conn)
    return {"as_of": as_of, "sets": sets, "rows": rows_out, "active": active}


def validation(conn, as_of: str | None = None) -> dict:
    """Alert quality under two label-timing policies, plus the label ledger.

    * ``on_time`` — only labels the team could have seen within ``HORIZON`` days
      of the alert opening. This is what a review meeting would have believed.
    * ``eventual`` — every label, whenever it arrived, judged by the outcome's
      business time (``effective_at``). This is what a backtest should use.
    * ``retracted_excluded`` — every label except withdrawn ones.

    The difference between the first two is exactly the value (or harm) of
    late / backdated labels; the ``flips`` list names each affected alert.
    """
    HORIZON = 30
    as_of = as_of_or_latest(conn, as_of)
    warnings = _warnings_at(conn, as_of)
    labels = _labels_at(conn, as_of)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM customer")}
    by_customer: dict[int, list[dict]] = {}
    for l in labels:
        by_customer.setdefault(l["customer_id"], []).append(l)

    def pick(cid: int, policy: str, opened_at: str) -> dict | None:
        if policy == "on_time":
            # What a review at this cut-off could have known: labels that had
            # arrived by then *and* within the alert's horizon.
            cutoff = min(add_days(opened_at, HORIZON), as_of)
            ls = [l for l in by_customer.get(cid, [])
                  if not l["retracted"] and l["available_at"] <= cutoff]
        elif policy == "include_retracted":
            ls = [l for l in by_customer.get(cid, []) if l["available_at"] <= as_of]
        else:  # eventual
            ls = [l for l in by_customer.get(cid, [])
                  if not l["retracted"] and l["available_at"] <= as_of]
        if not ls:
            return None
        # A later "unknown" placeholder never overrides a real outcome; among
        # real outcomes the highest label version wins.
        real = [l for l in ls if l["value"] != "unknown"]
        pool = real or ls
        return max(pool, key=lambda l: (l["label_version"], l["id"]))

    def judge(l: dict, first_seen_at: str) -> str:
        """hit | miss | unknown for a warning opened at first_seen_at."""
        if l["value"] == "unknown":
            return "unknown"
        # Outcome counts only if it happened on/after the alert opened.
        if l["value"] == "churned" and l["effective_at"][:10] >= first_seen_at[:10]:
            return "hit"
        return "miss"

    def evaluate(policy: str) -> dict:
        hit = miss = unlabeled = late_judged = 0
        for w in warnings:
            l = pick(w["customer_id"], policy, w["first_seen_at"])
            if l is None or l["value"] == "unknown":
                unlabeled += 1
                continue
            if l["backdated"]:
                late_judged += 1
            if judge(l, w["first_seen_at"]) == "hit":
                hit += 1
            else:
                miss += 1
        judged = hit + miss
        return {
            "policy": policy,
            "hit": hit,
            "miss": miss,
            "unlabeled": unlabeled,
            "judged": judged,
            "late_judged": late_judged,
            "precision": round(hit / judged, 3) if judged else None,
        }

    policies = [
        {"code": "on_time", "label": "告警后 30 天内已知标签",
         "note": "复盘中当时能看到的口径"},
        {"code": "eventual", "label": "全部标签（含迟到/回溯）",
         "note": "按业务时间判定结果真相"},
        {"code": "include_retracted", "label": "未剔除撤销标签",
         "note": "撤销的标签仍被当作真相"},
    ]
    eval_out = [dict(p, **evaluate(p["code"])) for p in policies]

    flips = []
    for w in warnings:
        on = pick(w["customer_id"], "on_time", w["first_seen_at"])
        ev = pick(w["customer_id"], "eventual", w["first_seen_at"])
        on_v = judge(on, w["first_seen_at"]) if on else "unknown"
        ev_v = judge(ev, w["first_seen_at"]) if ev else "unknown"
        if on_v == ev_v:
            continue
        flips.append({
            "warning_id": w["id"],
            "customer_id": w["customer_id"],
            "customer": w["customer_name"],
            "from": on_v,
            "to": ev_v,
            "label_id": ev["id"] if ev else None,
            "backdated": bool(ev["backdated"]) if ev else False,
            "effective_at": ev["effective_at"] if ev else None,
            "available_at": ev["available_at"] if ev else None,
            "delay_days": days_between(ev["available_at"], ev["effective_at"]) if ev else None,
            "detail": {
                ("unknown", "hit"): "迟到标签把当时无法判定的告警补判为命中",
                ("unknown", "miss"): "迟到标签把当时无法判定的告警补判为漏报",
                ("hit", "miss"): "回溯标签推翻了此前的命中",
                ("miss", "hit"): "回溯标签把此前的漏报改判为命中",
            }.get((on_v, ev_v), "标签时间口径改变判定"),
        })

    # Label ledger with the feedback / warning effects each label produced,
    # projected to the cut-off: a replay must not cite a closure it caused later.
    ledger = []
    projected = {w["id"]: w for w in warnings}
    feedback_at = _project_feedback(conn, as_of)
    by_label: dict[int, list[dict]] = {}
    for f in feedback_at:
        if f["outcome_label_id"] is not None:
            by_label.setdefault(f["outcome_label_id"], []).append(f)
    for l in labels:
        effects = []
        for f in by_label.get(l["id"], []):
            w = projected.get(f["warning_id"])
            effects.append({
                "id": f["id"], "verdict": f["verdict"], "outcome": f["outcome"],
                "outcome_retracted": f["outcome_retracted"], "warning_id": f["warning_id"],
                "warning_closed_at": w["closed_at"] if w else None,
                "warning_close_reason": w["close_reason"] if w else None,
            })
        ledger.append({
            "id": l["id"], "customer_id": l["customer_id"], "customer": names[l["customer_id"]],
            "label_version": l["label_version"], "value": l["value"],
            "effective_at": l["effective_at"], "available_at": l["available_at"],
            "backdated": bool(l["backdated"]), "retracted": bool(l["retracted"]),
            "source": l["source"], "note": l["note"],
            "delay_days": days_between(l["available_at"], l["effective_at"]),
            "known_at_as_of": l["available_at"] <= as_of,
            "retracts": l.get("retracts"),
            "effects": effects,
        })

    missing = _rows(conn.execute(
        """SELECT missing_reason, COUNT(*) n FROM feature_version
           WHERE value IS NULL AND status='active' AND available_at<=?
           GROUP BY missing_reason ORDER BY n DESC""", (as_of,)))
    for m in missing:
        m["label"] = MISSING_REASON_LABELS.get(m["missing_reason"], m["missing_reason"])

    late_revisions = _rows(conn.execute(
        """SELECT fv.*, c.name AS customer_name, f.label AS feature_label,
                  CAST((julianday(fv.available_at)-julianday(fv.effective_at)) AS INTEGER) AS gap_days
           FROM feature_version fv
           JOIN customer c ON c.id=fv.customer_id
           JOIN feature_def f ON f.code=fv.feature_code
           WHERE fv.available_at > fv.effective_at AND fv.available_at<=?
           ORDER BY gap_days DESC, fv.id DESC""", (as_of,)))
    return {
        "as_of": as_of,
        "horizon_days": HORIZON,
        "policies": eval_out,
        "flips": flips,
        "labels": ledger,
        "missing": missing,
        "late_revisions": late_revisions,
        "warning_count": len(warnings),
    }


def audit(conn) -> dict:
    return {"runs": runs_list(conn)["items"], "jobs": jobs_list(conn)["items"]}
