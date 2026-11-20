"""Seed data: a believable 12-customer book with revisions, late data and gaps.

What is seeded here is *data*, not behaviour: customers, feature revisions
(with their effective/available times), the feature catalog and two feature-set
versions. Merges, labels, scoring runs, alerts and feedback are replayed
chronologically by :mod:`churn.demo` so every one of them goes through the
engine and leaves an audit trail.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .catalog import FEATURES, feature_set_v1
from .store import utcnow

SEED_EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc)


def ts(day_offset: int, hour: int = 9, minute: int = 0) -> str:
    d = SEED_EPOCH + timedelta(days=day_offset, hours=hour, minutes=minute)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


CUSTOMERS = [
    (1, "北辰数据", "华东", "Enterprise", {"logins_30d": 6, "support_tickets_30d": 7, "seat_utilization": 0.22, "invoice_overdue_days": 42, "nps_survey": 3}),
    (2, "云帆科技", "华东", "Enterprise", {"logins_30d": 14, "support_tickets_30d": 2, "seat_utilization": 0.61, "invoice_overdue_days": 0, "nps_survey": 8}),
    (3, "澄海物流", "华南", "Mid-Market", {"logins_30d": 8, "support_tickets_30d": 5, "seat_utilization": 0.34, "invoice_overdue_days": 18, "nps_survey": 4}),
    (4, "原野零售", "华中", "Mid-Market", {"logins_30d": 20, "support_tickets_30d": 1, "seat_utilization": 0.78, "invoice_overdue_days": 0, "nps_survey": 9}),
    (5, "星桥教育", "华北", "SMB", {"logins_30d": 3, "support_tickets_30d": 9, "seat_utilization": 0.15, "invoice_overdue_days": 55, "nps_survey": 2}),
    (6, "临川医药", "西南", "Enterprise", {"logins_30d": 11, "support_tickets_30d": 4, "seat_utilization": 0.47, "invoice_overdue_days": 6, "nps_survey": 6}),
    (7, "白鹭文旅", "华东", "SMB", {"logins_30d": 5, "support_tickets_30d": 6, "seat_utilization": 0.27, "invoice_overdue_days": 28, "nps_survey": 4}),
    (8, "山河制造", "华北", "Enterprise", {"logins_30d": 16, "support_tickets_30d": 3, "seat_utilization": 0.64, "invoice_overdue_days": 4, "nps_survey": 7}),
    (9, "拾光传媒", "华南", "SMB", {"logins_30d": 9, "support_tickets_30d": 3, "seat_utilization": 0.41, "invoice_overdue_days": 11, "nps_survey": 5}),
    (10, "屹立能源", "西北", "Enterprise", {"logins_30d": 12, "support_tickets_30d": 2, "seat_utilization": 0.58, "invoice_overdue_days": 0, "nps_survey": 8}),
    (11, "临川医药(旧账套)", "西南", "Enterprise", {"logins_30d": 9, "support_tickets_30d": 5, "seat_utilization": 0.49, "invoice_overdue_days": 14, "nps_survey": None}),
    (12, "青禾食品", "华中", "Mid-Market", {"logins_30d": 13, "support_tickets_30d": 2, "seat_utilization": 0.55, "invoice_overdue_days": 3, "nps_survey": 7}),
]

SOURCE = {
    "logins_30d": "product_events",
    "support_tickets_30d": "support_system",
    "seat_utilization": "billing_provisioning",
    "invoice_overdue_days": "erp_billing",
    "nps_survey": "survey_platform",
}


def _noise(rng: random.Random, v: float, pct: float) -> float:
    return round(v * (1 + rng.uniform(-pct, pct)), 3)


def _rev(conn, cid: int, code: str, value, effective_at: str, available_at: str,
         *, missing_reason: str | None = None, note: str | None = None, created_at: str | None = None) -> None:
    conn.execute(
        """INSERT INTO feature_version(customer_id,feature_code,value,effective_at,available_at,
               status,missing_reason,source,note,created_at)
           VALUES(?,?,?,?,?, 'active', ?,?,?,?)""",
        (cid, code, value, effective_at, available_at,
         missing_reason, SOURCE[code], note, created_at or available_at),
    )


def seed_base(conn) -> dict:
    """Populate an empty database with customers, revisions and feature sets."""
    rng = random.Random(20261119)
    feature_set_v1(conn)
    now = utcnow()

    for cid, name, region, segment, _p in CUSTOMERS:
        conn.execute(
            "INSERT INTO customer(id,name,region,segment,created_at) VALUES(?,?,?,?,?)",
            (cid, name, region, segment, ts(-120, 8)),
        )

    # Baseline revisions: business time Oct 5, learned Oct 6 (before the W1 run).
    for cid, _n, _r, _s, profile in CUSTOMERS:
        for f in FEATURES:
            code = f["code"]
            v = profile[code]
            if v is None:
                _rev(conn, cid, code, None, ts(36, 10), ts(37, 10),
                     missing_reason="opt_out", note="客户未授权问卷数据")
                continue
            _rev(conn, cid, code, _noise(rng, v, 0.06), ts(34, 10), ts(35, 12))

    # Mid-period cycle refresh (effective Oct 20, available Oct 20 13:00).
    degrades = {1: 0.65, 3: 0.80, 5: 0.70, 7: 0.85}
    improves = {2: 1.25, 4: 1.15, 8: 1.10}
    profiles = {c: p for c, _n, _r, _s, p in CUSTOMERS}
    for cid, mult in {**degrades, **improves}.items():
        p = profiles[cid]
        for code in ("logins_30d", "support_tickets_30d", "seat_utilization"):
            v = p[code]
            if v is None:
                continue
            new = v * mult if code != "support_tickets_30d" else max(0.0, v * (2 - mult))
            _rev(conn, cid, code, round(new, 3), ts(49, 10), ts(49, 13), note="周期刷新")
        if cid in degrades:
            _rev(conn, cid, "invoice_overdue_days", round(p["invoice_overdue_days"] * 1.6, 2),
                 ts(50, 10), ts(50, 15), note="账期刷新")

    # Old ledger (customer 11) backfill available Oct 27, before the merge.
    _rev(conn, 11, "support_tickets_30d", 11.0, ts(56, 10), ts(56, 16), note="旧账套补录")

    # Late-arriving revision: the business fact is from Oct 28, but the platform
    # only learns it on Nov 4. Runs in between are built on the older Oct 20
    # value although fresher data already existed in business time.
    _rev(conn, 1, "seat_utilization", 0.12, ts(57, 10), ts(64, 9),
         note="上游改版后补数，业务上 10-28 已发生")

    # Missing-data cases with explicit reasons.
    _rev(conn, 7, "nps_survey", None, ts(44, 10), ts(44, 14),
         missing_reason="source_outage", note="问卷平台迁移，9 月批次丢失")
    _rev(conn, 9, "invoice_overdue_days", None, ts(40, 10), ts(40, 11),
         missing_reason="not_applicable", note="预付客户，无账期概念")
    _rev(conn, 12, "seat_utilization", None, ts(57, 10), ts(57, 12),
         missing_reason="below_maturity", note="开通不足 14 天，未满成熟窗口")

    # Feature-set v2 for the version-compare view (not activated).
    exists = conn.execute("SELECT 1 FROM feature_set WHERE version=2").fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO feature_set(version,name,notes,created_at,active) VALUES(2,?,?,?,0)",
            (
                "v2 行为加权",
                "把使用行为提到更前：登录 0.28 / 工单 0.26 / 席位 0.24 / 逾期 0.12 / NPS 0.10。",
                ts(75, 10),
            ),
        )
        v2 = {
            "logins_30d": 0.28,
            "support_tickets_30d": 0.26,
            "seat_utilization": 0.24,
            "invoice_overdue_days": 0.12,
            "nps_survey": 0.10,
        }
        for code, w in v2.items():
            conn.execute(
                "INSERT INTO feature_set_member(set_version,feature_code,weight) VALUES(2,?,?)",
                (code, w),
            )

    conn.commit()
    return {"customers": len(CUSTOMERS), "now": now}
