"""Shared helpers for the stdlib test suite."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from churn.catalog import FEATURES, feature_set_v1
from churn.store import connect, init_db

EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc)


def ts(day: int, hour: int = 9, minute: int = 0) -> str:
    """Business timestamp: 2026-09-01 + day offset."""
    d = EPOCH + timedelta(days=day, hours=hour, minutes=minute)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


SOURCE = {
    "logins_30d": "product_events",
    "support_tickets_30d": "support_system",
    "seat_utilization": "billing_provisioning",
    "invoice_overdue_days": "erp_billing",
    "nps_survey": "survey_platform",
}

# A neutral profile: every feature sits on its anchor, so the score is 50.
NEUTRAL = {
    "logins_30d": 10.0,
    "support_tickets_30d": 3.0,
    "seat_utilization": 0.45,
    "invoice_overdue_days": 10.0,
    "nps_survey": 6.0,
}

# A customer in trouble: each feature pushes risk up.
RISKY = {
    "logins_30d": 2.0,
    "support_tickets_30d": 9.0,
    "seat_utilization": 0.10,
    "invoice_overdue_days": 60.0,
    "nps_survey": 1.0,
}


class DbCase(unittest.TestCase):
    """Temp database + a tiny builder for controlled fixtures."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="churn-test-")
        self.path = f"{self.tmpdir}/test.sqlite"
        self.conn = connect(self.path)
        init_db(self.conn)
        feature_set_v1(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------
    def customer(self, cid: int, name: str | None = None, segment: str = "Enterprise") -> int:
        self.conn.execute(
            "INSERT INTO customer(id,name,region,segment,created_at) VALUES(?,?,?,?,?)",
            (cid, name or f"客户{cid}", "华东", segment, ts(-30)),
        )
        return cid

    def rev(self, cid: int, code: str, value, eff: str, avail: str | None = None,
            reason: str | None = None, note: str | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO feature_version(customer_id,feature_code,value,effective_at,available_at,
                   status,missing_reason,source,note,created_at)
               VALUES(?,?,?,?,?,'active',?,?,?,?)""",
            (cid, code, value, eff, avail or eff, reason, SOURCE[code], note, avail or eff),
        )
        self.conn.commit()
        return cur.lastrowid

    def profile(self, cid: int, values: dict, eff: str, avail: str | None = None) -> int:
        last = None
        for f in FEATURES:
            last = self.rev(cid, f["code"], values[f["code"]], eff, avail)
        return last

    def latest_score(self, customer_id: int) -> dict | None:
        row = self.conn.execute(
            """SELECT cs.*, r.as_of FROM customer_score cs JOIN scoring_run r ON r.id=cs.run_id
               WHERE cs.customer_id=? ORDER BY r.as_of DESC, r.id DESC LIMIT 1""",
            (customer_id,),
        ).fetchone()
        return dict(row) if row else None

    def open_warning(self, customer_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM warning WHERE customer_id=? AND closed_at IS NULL", (customer_id,)
        ).fetchone()
        return dict(row) if row else None

    def events(self, warning_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM warning_event WHERE warning_id=? ORDER BY at, id", (warning_id,))]
