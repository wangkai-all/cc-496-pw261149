"""Score math, no-look-ahead visibility, missing reasons and staleness."""

from __future__ import annotations

import unittest

from churn.catalog import tier_for
from churn.engine import run_scoring
from churn.scoring import explain, score_entity
from tests.support import NEUTRAL, RISKY, DbCase, ts


class ScoreMathTest(DbCase):
    def test_neutral_profile_scores_fifty(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        res = score_entity(self.conn, 1, ts(12))
        self.assertEqual(res.score, 50.0)
        self.assertEqual(res.tier, "medium")
        self.assertEqual(len(res.evidence), 5)
        for ev in res.evidence:
            self.assertEqual(ev["contribution"], 0.0)

    def test_risky_profile_is_critical_and_contributions_add_up(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        res = score_entity(self.conn, 1, ts(12))
        self.assertEqual(res.tier, "critical")
        self.assertAlmostEqual(res.score, 50.0 + sum(e["contribution"] for e in res.evidence), places=2)
        # every contribution is signed: risk up is positive
        self.assertTrue(all(e["contribution"] > 0 for e in res.evidence))

    def test_contribution_is_weight_times_direction_times_clamped_z(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        # logins: anchor 10, scale 10, weight 0.30, lower_risk -> 4 logins = z -0.6
        self.rev(1, "logins_30d", 4.0, ts(11))
        res = score_entity(self.conn, 1, ts(12))
        ev = {e["code"]: e for e in res.evidence}["logins_30d"]
        self.assertAlmostEqual(ev["z"], -0.6)
        self.assertAlmostEqual(ev["contribution"], round(0.30 * -1.0 * -0.6 * 50, 2))
        # beyond the clamp the contribution stops growing
        self.rev(1, "logins_30d", -100.0, ts(12))
        ev = {e["code"]: e for e in score_entity(self.conn, 1, ts(13)).evidence}["logins_30d"]
        self.assertEqual(ev["z"], -1.0)
        self.assertAlmostEqual(ev["contribution"], 15.0)

    def test_score_is_clamped_to_0_100(self):
        self.customer(1)
        self.profile(1, {k: v * 10 if k != "seat_utilization" else 10.0 for k, v in RISKY.items()}, ts(10))
        res = score_entity(self.conn, 1, ts(12))
        self.assertLessEqual(res.score, 100.0)
        self.assertGreaterEqual(res.score, 0.0)

    def test_tier_boundaries(self):
        self.assertEqual(tier_for(75.0), "critical")
        self.assertEqual(tier_for(74.99), "high")
        self.assertEqual(tier_for(55.0), "high")
        self.assertEqual(tier_for(54.99), "medium")
        self.assertEqual(tier_for(35.0), "medium")
        self.assertEqual(tier_for(34.99), "low")
        self.assertEqual(tier_for(0.0), "low")


class NoLookAheadTest(DbCase):
    def test_revision_after_cutoff_is_ignored(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        # A much worse revision, business time day 11, but only learned day 20.
        self.rev(1, "logins_30d", 1.0, ts(11), ts(20))
        before = score_entity(self.conn, 1, ts(15))
        ev = {e["code"]: e for e in before.evidence}["logins_30d"]
        self.assertEqual(ev["value"], NEUTRAL["logins_30d"])
        self.assertEqual(before.score, 50.0)
        # After the cut-off reaches the availability, the revision applies:
        # fewer logins means more risk, so the score goes up.
        after = score_entity(self.conn, 1, ts(21))
        self.assertGreater(after.score, before.score)

    def test_late_fact_is_flagged_but_not_scored(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        self.rev(1, "seat_utilization", 0.05, ts(18), ts(22))  # business day 18, learned day 22
        res = score_entity(self.conn, 1, ts(19))
        ev = {e["code"]: e for e in res.evidence}["seat_utilization"]
        self.assertEqual(ev["value"], NEUTRAL["seat_utilization"])
        self.assertTrue(ev["late"])
        self.assertEqual(ev["late_days"], 3)  # 19 -> 22 is 3 days
        self.assertEqual(res.late_features, 1)

    def test_future_business_time_is_not_late(self):
        """A revision whose own business time is after the cut-off does not exist yet."""
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        self.rev(1, "seat_utilization", 0.05, ts(25), ts(22))
        res = score_entity(self.conn, 1, ts(19))
        ev = {e["code"]: e for e in res.evidence}["seat_utilization"]
        self.assertFalse(ev["late"])
        self.assertEqual(res.late_features, 0)

    def test_late_run_is_visible_at_the_later_cutoff(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        self.rev(1, "invoice_overdue_days", 0.0, ts(13), ts(30))
        old = score_entity(self.conn, 1, ts(12))
        new = score_entity(self.conn, 1, ts(31))
        self.assertGreater(old.score, new.score)  # overdue days resolved

    def test_never_collected_feature_contributes_zero_with_reason(self):
        """No revision at all is not the same as "value 0"."""
        self.customer(1)
        for code, value in NEUTRAL.items():
            if code != "nps_survey":
                self.rev(1, code, value, ts(10))
        res = score_entity(self.conn, 1, ts(12))
        self.assertIn("nps_survey", res.missing)
        self.assertEqual(res.missing["nps_survey"]["reason"], "not_collected")
        self.assertAlmostEqual(res.score, 50.0)


class MissingAndStaleTest(DbCase):
    def test_missing_reason_is_carried_with_source(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        self.rev(1, "nps_survey", None, ts(11), reason="opt_out")
        res = score_entity(self.conn, 1, ts(12))
        self.assertIn("nps_survey", res.missing)
        self.assertEqual(res.missing["nps_survey"]["reason"], "opt_out")
        self.assertEqual(res.missing["nps_survey"]["reason_label"], "客户拒绝")
        self.assertEqual(res.missing["nps_survey"]["source"], "survey_platform")
        self.assertEqual(res.score, 50.0)

    def test_missing_value_without_reason_defaults_to_not_collected(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        self.rev(1, "nps_survey", None, ts(11))
        res = score_entity(self.conn, 1, ts(12))
        self.assertEqual(res.missing["nps_survey"]["reason"], "not_collected")

    def test_pending_backfill_before_first_sighting(self):
        """A revision that exists upstream but arrived after the cut-off."""
        self.customer(1)
        for code, value in NEUTRAL.items():
            if code != "nps_survey":
                self.rev(1, code, value, ts(10))
        self.rev(1, "nps_survey", 3.0, ts(9), ts(20))
        res = score_entity(self.conn, 1, ts(12))
        self.assertEqual(res.missing["nps_survey"]["reason"], "pending_backfill")
        self.assertEqual(res.score, 50.0)

    def test_stale_threshold(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(0))
        res = score_entity(self.conn, 1, ts(40))
        self.assertEqual(res.stale_features, 0)
        res = score_entity(self.conn, 1, ts(46))
        self.assertEqual(res.stale_features, 5)
        for ev in res.evidence:
            self.assertEqual(ev["stale_days"], 46)

    def test_explain_sorts_by_absolute_contribution(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        out = explain(self.conn, 1, ts(12))
        contributions = [abs(e["contribution"]) for e in out["evidence"]]
        self.assertEqual(contributions, sorted(contributions, reverse=True))
        self.assertTrue(all(e["summary"] for e in out["evidence"]))

    def test_explain_names_missing_reason_in_plain_words(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        self.rev(1, "seat_utilization", None, ts(10), reason="source_outage")
        out = explain(self.conn, 1, ts(12))
        item = next(e for e in out["evidence"] if e["code"] == "seat_utilization")
        self.assertIn("缺失", item["summary"])
        self.assertIn("上游故障", item["summary"])


class RunRecordTest(DbCase):
    def test_run_records_counts_and_watermark(self):
        for cid in (1, 2):
            self.customer(cid)
            self.profile(cid, NEUTRAL, ts(10))
        out = run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        run = self.conn.execute("SELECT * FROM scoring_run WHERE id=?", (out["run_id"],)).fetchone()
        self.assertEqual(run["scored_count"], 2)
        self.assertEqual(run["scope"], "full")
        self.assertEqual(run["watermark"], ts(10))
        rows = self.conn.execute(
            "SELECT COUNT(*) n FROM customer_score WHERE run_id=?", (out["run_id"],)).fetchone()
        self.assertEqual(rows["n"], 2)

    def test_scores_are_immutable_per_run(self):
        self.customer(1)
        self.profile(1, NEUTRAL, ts(10))
        first = run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        self.rev(1, "logins_30d", 1.0, ts(13))
        second = run_scoring(self.conn, as_of=ts(14), trigger="manual", run_at=ts(14, 1))
        old = self.conn.execute(
            "SELECT score FROM customer_score WHERE run_id=?", (first["run_id"],)).fetchone()
        new = self.conn.execute(
            "SELECT score FROM customer_score WHERE run_id=?", (second["run_id"],)).fetchone()
        self.assertEqual(old["score"], 50.0)
        self.assertGreater(new["score"], 50.0)
        history = self.conn.execute("SELECT COUNT(*) n FROM customer_score").fetchone()
        self.assertEqual(history["n"], 2)


if __name__ == "__main__":
    unittest.main()
