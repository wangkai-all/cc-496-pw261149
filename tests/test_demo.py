"""Demo timeline: a deterministic replay that exercises every mechanic end to end."""

from __future__ import annotations

import contextlib
import io
import unittest

from churn import queries
from churn.demo import build_demo
from tests.support import DbCase


class DemoTimelineTest(DbCase):
    def setUp(self):
        super().setUp()
        with contextlib.redirect_stdout(io.StringIO()):
            self.summary = build_demo(self.conn)

    def test_replay_is_complete(self):
        self.assertEqual(self.summary["runs"], 13)
        self.assertEqual(self.summary["jobs"], 8)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM customer").fetchone()["n"], 12)

    def test_every_alert_change_has_an_event(self):
        orphans = self.conn.execute(
            """SELECT COUNT(*) n FROM warning w
               WHERE w.opened_run_id IS NULL
                  OR NOT EXISTS (SELECT 1 FROM warning_event e WHERE e.warning_id=w.id)"""
        ).fetchone()["n"]
        self.assertEqual(orphans, 0)

    def test_merge_and_undo_are_both_on_record(self):
        merges = self.conn.execute("SELECT * FROM customer_merge ORDER BY id").fetchall()
        self.assertEqual(len(merges), 1)
        self.assertIsNotNone(merges[0]["undone_at"])
        self.assertIsNone(queries.bootstrap(self.conn)["customers"][10]["merged_into"])

    def test_label_ledger_has_late_and_retracted_rows(self):
        labels = queries.validation(self.conn)["labels"]
        self.assertTrue(any(l["backdated"] for l in labels))
        self.assertTrue(any(l["retracted"] for l in labels))
        self.assertTrue(any(l["value"] == "retained" for l in labels))

    def test_policies_differ(self):
        by = {p["policy"]: p for p in queries.validation(self.conn)["policies"]}
        self.assertNotEqual(by["on_time"]["precision"], by["eventual"]["precision"])
        self.assertLess(by["eventual"]["precision"], by["on_time"]["precision"])

    def test_dismissal_was_overturned_by_the_late_label(self):
        w1 = self.conn.execute("SELECT * FROM warning WHERE id=1").fetchone()
        self.assertEqual(w1["close_reason"], "dismissed")
        opened = self.conn.execute(
            "SELECT * FROM warning WHERE customer_id=1 AND closed_at IS NULL").fetchone()
        self.assertEqual(opened["supersedes"], 1)
        refreshed = self.conn.execute(
            """SELECT COUNT(*) n FROM warning_event
               WHERE warning_id=? AND kind='refreshed' AND detail LIKE '%推翻%'""",
            (opened["id"],)).fetchone()["n"]
        self.assertEqual(refreshed, 1)

    def test_deduped_events_accumulate(self):
        deduped = self.conn.execute(
            "SELECT COUNT(*) n FROM warning_event WHERE kind='deduped'").fetchone()["n"]
        self.assertGreater(deduped, 0)
        open_count = self.conn.execute(
            "SELECT COUNT(*) n FROM warning WHERE closed_at IS NULL").fetchone()["n"]
        self.assertEqual(
            open_count,
            self.conn.execute(
                "SELECT COUNT(DISTINCT customer_id) n FROM warning WHERE closed_at IS NULL"
            ).fetchone()["n"])

    def test_historical_alert_views_do_not_leak_the_future(self):
        early = "2026-10-07T01:00:00Z"
        _, deep = None, None
        from churn import queries as q
        out = q.warnings_list(self.conn, "all", early)
        self.assertTrue(out["items"])
        self.assertTrue(all(w["first_seen_at"] <= early for w in out["items"]))
        port = q.portfolio(self.conn, early)
        for item in port["items"]:
            if item["warning"]:
                warning = self.conn.execute(
                    "SELECT first_seen_at FROM warning WHERE id=?",
                    (item["warning"]["id"],)).fetchone()
                self.assertLessEqual(warning["first_seen_at"], early)
        val = q.validation(self.conn, early)
        self.assertTrue(all(
            f["warning_id"] in {w["id"] for w in q.warnings_list(self.conn, "all", early)["items"]}
            for f in val["flips"]))

    def test_historical_cutoff_shows_the_alerts_of_that_moment(self):
        """10-21 shows no 10-23 dismissal and no 11-03 merge closure."""
        at_1021 = {w["id"]: w for w in queries.warnings_list(
            self.conn, "all", "2026-10-21T12:00:00Z")["items"]}
        self.assertEqual(at_1021[1]["close_reason"], None)
        self.assertEqual(at_1021[5]["close_reason"], None)
        self.assertIsNone(at_1021[4]["closed_at"])  # 白鹭 still open
        at_1105 = {w["id"]: w for w in queries.warnings_list(
            self.conn, "all", "2026-11-05T00:00:00Z")["items"]}
        self.assertEqual(at_1105[1]["close_reason"], "dismissed")
        self.assertEqual(at_1105[5]["close_reason"], "merged")
        # the successor alert (#7) opened 10-29 and is already on the day-05 view
        self.assertIn(7, at_1105)
        self.assertTrue(at_1105[7]["supersedes"] == 1)

    def test_historical_validation_uses_only_labels_known_then(self):
        early = queries.validation(self.conn, "2026-10-21T12:00:00Z")
        self.assertTrue(all(p["unlabeled"] == 5 for p in early["policies"]))
        self.assertEqual(early["flips"], [])
        self.assertTrue(all(not l["known_at_as_of"] for l in early["labels"]))

    def test_historical_customer_detail_merges_are_projected(self):
        day_8 = queries.customer_detail(self.conn, 6, "2026-11-08T00:00:00Z")
        self.assertTrue(any(not m["undone"] for m in day_8["merges"]))
        day_12 = queries.customer_detail(self.conn, 6, "2026-11-12T00:00:00Z")
        self.assertTrue(all(m["undone"] for m in day_12["merges"]))

    def test_historical_portfolio_is_stable(self):
        """Querying an old cut-off returns the tiers of that moment, not today's."""
        early = queries.portfolio(self.conn, "2026-10-07T00:00:00Z")
        late = queries.portfolio(self.conn)
        self.assertNotEqual(early["counts"], late["counts"])
        # Critical count can only grow as bad data arrives, never be rewritten.
        self.assertLessEqual(early["counts"]["critical"], late["counts"]["critical"])

    def test_detail_page_has_explanations_for_every_entity(self):
        for row in queries.bootstrap(self.conn)["customers"]:
            detail = queries.customer_detail(self.conn, row["id"])
            self.assertEqual(len(detail["explanation"]["evidence"]), 5)
            self.assertIn(detail["explanation"]["tier"], ("critical", "high", "medium", "low"))

    def test_version_compare_has_two_sets(self):
        out = queries.version_compare(self.conn)
        self.assertEqual([s["version"] for s in out["sets"]], [1, 2])
        self.assertTrue(all(len(r["versions"]) == 2 for r in out["rows"]))
        some_diff = any(
            r["versions"][2]["delta"] != 0 for r in out["rows"]
            if r["versions"][2].get("delta") is not None)
        self.assertTrue(some_diff, "v2 weights should move at least one score")

    def test_runs_and_jobs_are_listed(self):
        self.assertEqual(len(queries.runs_list(self.conn)["items"]), 13)
        jobs = queries.jobs_list(self.conn)["items"]
        self.assertEqual(len(jobs), 8)
        self.assertTrue(all("affected" in j and "result" in j for j in jobs))
        kinds = {j["kind"] for j in jobs}
        self.assertTrue({"backfill", "merge", "label_replay"} <= kinds)


if __name__ == "__main__":
    unittest.main()
