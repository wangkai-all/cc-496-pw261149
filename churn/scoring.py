"""Scoring engine.

The model is a transparent, additive risk score so every tier can be explained
by a short evidence chain:

    contribution_i = weight_i * direction_i * clamp((value_i - anchor_i) / scale_i, -1, 1) * 50

Missing features contribute 0 but are recorded with their reason and staleness.
A revision is *visible* to a run when ``available_at <= run.as_of`` (no
look-ahead). The most recent visible revision wins; its ``effective_at`` drives
the data-time / staleness display, and a revision whose ``available_at`` is
after ``as_of`` is reported as "late" for that run.

Merged customers are one entity: the survivor scores over the union of feature
revisions of every id that resolved to it at ``as_of``, so a merge can genuinely
move the score. History rows for the retired id are never rewritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .catalog import FEATURE_PARAMS, MISSING_REASON_LABELS, tier_for
from .store import days_between

STALE_DAYS = 45
ALERT_TIERS = ("critical", "high")


@dataclass
class FeatureView:
    code: str
    value: float | None
    effective_at: str | None
    available_at: str | None
    missing_reason: str | None
    source: str | None
    revision_id: int | None
    stale_days: int | None
    is_late: bool
    late_days: int | None
    owner_id: int | None


@dataclass
class ScoreResult:
    score: float
    tier: str
    evidence: list[dict]
    missing: dict
    stale_features: int
    late_features: int
    feature_set_version: int
    feature_views: list[FeatureView]


def current_feature_set(conn) -> int:
    row = conn.execute(
        "SELECT version FROM feature_set WHERE active=1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return row["version"]


def feature_weights(conn, set_version: int) -> dict[str, float]:
    rows = conn.execute(
        "SELECT feature_code, weight FROM feature_set_member WHERE set_version=?",
        (set_version,),
    ).fetchall()
    return {r["feature_code"]: r["weight"] for r in rows}


def resolve_entity(conn, customer_id: int, as_of: str | None = None) -> tuple[int, list[dict]]:
    """Follow customer_merge chains active at ``as_of``.

    Returns (survivor_id, chain). A merge only applies when it was known before
    as_of (``available_at <= as_of``); undoing a merge removes it from the chain.
    """
    chain: list[dict] = []
    seen: set[int] = set()
    current = customer_id
    while True:
        row = None
        if as_of:
            row = conn.execute(
                """SELECT m.*, cf.name AS from_name, ct.name AS to_name
                   FROM customer_merge m
                   JOIN customer cf ON cf.id=m.from_customer
                   JOIN customer ct ON ct.id=m.to_customer
                   WHERE m.from_customer=?
                     AND (m.undone_at IS NULL OR m.undone_at>?)
                     AND m.available_at<=?
                   ORDER BY m.available_at DESC, m.id DESC LIMIT 1""",
                (current, as_of, as_of),
            ).fetchone()
        if not row or row["to_customer"] in seen or row["to_customer"] == current:
            break
        seen.add(current)
        chain.append(
            {
                "merge_id": row["id"],
                "from": row["from_customer"],
                "to": row["to_customer"],
                "from_name": row["from_name"],
                "to_name": row["to_name"],
                "effective_at": row["effective_at"],
                "available_at": row["available_at"],
                "reason": row["reason"],
            }
        )
        current = row["to_customer"]
    return current, chain


def entity_ids(conn, entity_id: int, as_of: str) -> list[int]:
    """All ids that had resolved into ``entity_id`` by ``as_of`` (reverse merge)."""
    ids = [entity_id]
    frontier = [entity_id]
    seen = {entity_id}
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"""SELECT from_customer FROM customer_merge
                WHERE to_customer IN ({marks}) AND (undone_at IS NULL OR undone_at>?)
                  AND available_at<=?""",
            (*frontier, as_of, as_of),
        ).fetchall()
        nxt = []
        for r in rows:
            cid = r["from_customer"]
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
                nxt.append(cid)
        frontier = nxt
    return ids


def aliases_of(conn, customer_id: int, as_of: str) -> tuple[int, list[int], list[dict]]:
    entity, chain = resolve_entity(conn, customer_id, as_of)
    return entity, entity_ids(conn, entity, as_of), chain


def _pick_revision(conn, owner_ids: list[int], code: str, as_of: str):
    """Choose the revision a run at ``as_of`` may use, plus any newer pending one.

    The chosen row is the most recent revision by ``effective_at`` among those
    with ``available_at <= as_of`` (no look-ahead). The second value is a
    late-arriving *newer* revision: business time at or before the cut-off, but
    learned only afterwards. It is never used for the score; it only marks the
    evidence as built without information that, in business time, already
    existed.
    """
    marks = ",".join("?" * len(owner_ids))
    # Take the newest visible revision of each aliased ledger first, then merge
    # them: a ledger that never collected a feature ("missing") must not mask a
    # value another ledger of the same entity did provide. Within one ledger the
    # newest revision still wins, missing or not.
    visible = conn.execute(
        f"""SELECT * FROM (
                SELECT fv.*, ROW_NUMBER() OVER (
                    PARTITION BY fv.customer_id
                    ORDER BY fv.effective_at DESC, fv.id DESC) AS rn
                FROM feature_version fv
                WHERE fv.customer_id IN ({marks}) AND fv.feature_code=?
                  AND fv.status='active' AND fv.available_at<=?
            ) WHERE rn=1
            ORDER BY (value IS NULL), effective_at DESC, id DESC LIMIT 1""",
        (*owner_ids, code, as_of),
    ).fetchone()
    latest = conn.execute(
        f"""SELECT * FROM feature_version
            WHERE customer_id IN ({marks}) AND feature_code=? AND status='active'
            ORDER BY effective_at DESC, id DESC LIMIT 1""",
        (*owner_ids, code),
    ).fetchone()
    # Late-arriving facts: business time at or before the cut-off, but only
    # learned after it. Only a fact *newer* than the one used matters — it means
    # this run is built on older data although fresher data already existed.
    pending = conn.execute(
        f"""SELECT * FROM feature_version
            WHERE customer_id IN ({marks}) AND feature_code=? AND status='active'
              AND effective_at<=? AND available_at>?
            ORDER BY effective_at DESC, id DESC LIMIT 1""",
        (*owner_ids, code, as_of, as_of),
    ).fetchone()
    if pending and visible and pending["effective_at"] <= visible["effective_at"]:
        pending = None
    return visible, pending


def feature_views(conn, owner_ids: list[int], set_version: int, as_of: str) -> list[FeatureView]:
    codes = [
        r["feature_code"]
        for r in conn.execute(
            "SELECT feature_code FROM feature_set_member WHERE set_version=? ORDER BY weight DESC",
            (set_version,),
        ).fetchall()
    ]
    views: list[FeatureView] = []
    for code in codes:
        row, pending = _pick_revision(conn, owner_ids, code, as_of)
        if row is None:
            # Nothing had arrived by the cut-off. If a revision exists upstream
            # it is still in flight; otherwise the feature was never collected.
            # Either way it contributes 0 and is never filled from the future.
            if pending:
                views.append(FeatureView(
                    code, None, pending["effective_at"], pending["available_at"],
                    "pending_backfill", pending["source"], None, None,
                    False, days_between(pending["available_at"], as_of), pending["customer_id"],
                ))
            else:
                views.append(
                    FeatureView(code, None, None, None, "not_collected", None, None, None, False, None, None)
                )
            continue
        value = row["value"]
        reason = row["missing_reason"]
        if value is None and not reason:
            reason = "not_collected"
        stale = days_between(as_of, row["effective_at"]) if row["effective_at"] else None
        # >0 means a newer point existed upstream but was only learned after the cut-off
        late_days = days_between(pending["available_at"], as_of) if pending else None
        views.append(
            FeatureView(
                code=code,
                value=value,
                effective_at=row["effective_at"],
                available_at=row["available_at"],
                missing_reason=None if value is not None else reason,
                source=row["source"],
                revision_id=row["id"],
                stale_days=stale,
                is_late=(late_days or 0) > 0,
                late_days=late_days,
                owner_id=row["customer_id"],
            )
        )
    return views


def score_entity(conn, entity_id: int, as_of: str, set_version: int | None = None) -> ScoreResult:
    entity, owner_ids, _chain = aliases_of(conn, entity_id, as_of)
    set_version = set_version or current_feature_set(conn)
    weights = feature_weights(conn, set_version)
    directions = {
        r["code"]: (1.0 if r["direction"] == "higher_risk" else -1.0)
        for r in conn.execute("SELECT code, direction FROM feature_def").fetchall()
    }
    views = feature_views(conn, owner_ids, set_version, as_of)
    evidence: list[dict] = []
    missing: dict[str, dict] = {}
    total = 0.0
    for v in views:
        params = FEATURE_PARAMS[v.code]
        weight = weights.get(v.code, 0.0)
        direction = directions.get(v.code, 1.0)
        if v.value is None:
            missing[v.code] = {
                "reason": v.missing_reason,
                "reason_label": MISSING_REASON_LABELS.get(v.missing_reason or "", v.missing_reason),
                "source": v.source,
                "revision_id": v.revision_id,
                "stale_days": v.stale_days,
                "owner_id": v.owner_id,
            }
            evidence.append(
                {
                    "code": v.code,
                    "value": None,
                    "weight": weight,
                    "contribution": 0.0,
                    "z": None,
                    "note": "missing",
                    "owner_id": v.owner_id,
                }
            )
            continue
        z = max(-1.0, min(1.0, (v.value - params["anchor"]) / params["scale"]))
        contribution = round(weight * direction * z * 50.0, 2)
        total += contribution
        evidence.append(
            {
                "code": v.code,
                "value": v.value,
                "weight": weight,
                "z": round(z, 3),
                "contribution": contribution,
                "effective_at": v.effective_at,
                "available_at": v.available_at,
                "stale_days": v.stale_days,
                "revision_id": v.revision_id,
                "late": v.is_late,
                "late_days": v.late_days,
                "owner_id": v.owner_id,
            }
        )
    score = round(max(0.0, min(100.0, 50.0 + total)), 2)
    stale_features = sum(1 for v in views if v.stale_days is not None and v.stale_days > STALE_DAYS)
    late_features = sum(1 for v in views if v.is_late)
    return ScoreResult(
        score=score,
        tier=tier_for(score),
        evidence=evidence,
        missing=missing,
        stale_features=stale_features,
        late_features=late_features,
        feature_set_version=set_version,
        feature_views=views,
    )


def explain(conn, entity_id: int, as_of: str, set_version: int | None = None) -> dict:
    """Human-readable score explanation with the evidence chain in plain language."""
    set_version = set_version or current_feature_set(conn)
    res = score_entity(conn, entity_id, as_of, set_version)
    defs = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM feature_def").fetchall()}
    items = []
    for ev in res.evidence:
        d = defs[ev["code"]]
        if ev["value"] is None:
            m = res.missing.get(ev["code"], {})
            items.append(
                {
                    "code": ev["code"],
                    "label": d["label"],
                    "value": None,
                    "contribution": 0.0,
                    "summary": f"{d['label']}缺失（{m.get('reason_label','未知原因')}），未计入本次评分",
                    "missing_reason": m.get("reason"),
                    "missing_reason_label": m.get("reason_label"),
                    "stale_days": m.get("stale_days"),
                    "source": m.get("source"),
                }
            )
            continue
        arrow = "↑风险" if ev["contribution"] > 0 else ("↓风险" if ev["contribution"] < 0 else "中性")
        items.append(
            {
                "code": ev["code"],
                "label": d["label"],
                "unit": d["unit"],
                "value": ev["value"],
                "contribution": ev["contribution"],
                "weight": ev["weight"],
                "summary": f"{d['label']} {ev['value']}{d['unit']}，{arrow} {abs(ev['contribution']):.1f} 分",
                "effective_at": ev.get("effective_at"),
                "available_at": ev.get("available_at"),
                "stale_days": ev.get("stale_days"),
                "late": ev.get("late"),
                "late_days": ev.get("late_days"),
                "owner_id": ev.get("owner_id"),
            }
        )
    items.sort(key=lambda x: abs(x["contribution"]), reverse=True)
    return {
        "score": res.score,
        "tier": res.tier,
        "evidence": items,
        "missing": res.missing,
        "stale_features": res.stale_features,
        "late_features": res.late_features,
        "feature_set_version": set_version,
        "as_of": as_of,
        "entity_id": entity_id,
    }


def json_evidence(ev: list[dict]) -> str:
    return json.dumps(ev, ensure_ascii=False, separators=(",", ":"))


def json_missing(m: dict) -> str:
    return json.dumps(m, ensure_ascii=False, separators=(",", ":"))
