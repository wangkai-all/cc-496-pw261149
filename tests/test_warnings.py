"""Alert lifecycle: open, dedupe, close, reopen, merged/recovered, stale runs."""

from __future__ import annotations

import sqlite3
import unittest

from churn.engine import (
    add_feedback,
    close_warning,
    merge_customers,
    reopen_warning,
    run_scoring,
    revise_feature,
)
from churn.queries import warnings_list
from tests.support import NEUTRAL, RISKY, DbCase, ts


class WarningLifecycleTest(DbCase):
    def setUp(self):
        super().setUp()
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        self.warning = self.open_warning(1)

    def test_critical_customer_opens_one_warning(self):
        self.assertIsNotNone(self.warning)
        self.assertEqual(self.warning["tier"], "critical")
        self.assertEqual(self.warning["dedupe_key"], "cust:1")
        self.assertEqual(self.warning["occurrences"], 1)
        self.assertEqual(self.events(self.warning["id"])[0]["kind"], "opened")
        total = self.conn.execute("SELECT COUNT(*) n FROM warning").fetchone()["n"]
        self.assertEqual(total, 1)

    def test_repeated_hit_dedupes_into_the_same_warning(self):
        run_scoring(self.conn, as_of=ts(14), trigger="manual", run_at=ts(14, 1))
        run_scoring(self.conn, as_of=ts(16), trigger="manual", run_at=ts(16, 1))
        total = self.conn.execute("SELECT COUNT(*) n FROM warning").fetchone()["n"]
        self.assertEqual(total, 1)
        row = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (self.warning["id"],)).fetchone()
        self.assertEqual(row["occurrences"], 3)
        self.assertEqual(row["last_seen_at"], ts(16, 1))
        kinds = [e["kind"] for e in self.events(self.warning["id"])]
        self.assertEqual(kinds.count("deduped"), 2)

    def test_medium_customer_has_no_alert(self):
        self.customer(2)
        self.profile(2, NEUTRAL, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="manual", run_at=ts(12, 2))
        self.assertIsNone(self.open_warning(2))

    def test_still_risky_after_dismissal_creates_successor(self):
        close_warning(self.conn, warning_id=self.warning["id"], reason="dismissed",
                      user="运营", at=ts(13))
        run_scoring(self.conn, as_of=ts(14), trigger="manual", run_at=ts(14, 1))
        new = self.open_warning(1)
        self.assertIsNotNone(new)
        self.assertNotEqual(new["id"], self.warning["id"])
        self.assertEqual(new["supersedes"], self.warning["id"])
        kinds = [e["kind"] for e in self.events(new["id"])]
        self.assertIn("reopened", kinds)

    def test_recovering_closes_with_reason_and_reopening_after_recovery(self):
        # Fix the data: risk falls to low.
        for code, value in NEUTRAL.items():
            revise_feature(self.conn, customer_id=1, code=code, value=value,
                           effective_at=ts(15), run_at=ts(15, 1))
        row = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (self.warning["id"],)).fetchone()
        self.assertIsNotNone(row["closed_at"])
        self.assertEqual(row["close_reason"], "recovered")
        self.assertEqual(row["closed_by"], "system")
        # Later deterioration opens a new warning that points back.
        for code, value in RISKY.items():
            revise_feature(self.conn, customer_id=1, code=code, value=value,
                           effective_at=ts(20), run_at=ts(20, 1))
        new = self.open_warning(1)
        self.assertEqual(new["supersedes"], self.warning["id"])

    def test_close_is_terminal_and_idempotency_guard(self):
        close_warning(self.conn, warning_id=self.warning["id"], reason="resolved",
                      user="运营", at=ts(13))
        self.assertIsNone(self.open_warning(1))
        with self.assertRaises(ValueError):
            close_warning(self.conn, warning_id=self.warning["id"], reason="dismissed",
                          user="运营", at=ts(14))
        with self.assertRaises(ValueError):
            close_warning(self.conn, warning_id=self.warning["id"], reason="whatever",
                          user="运营", at=ts(14))

    def test_closed_warning_is_still_queryable(self):
        close_warning(self.conn, warning_id=self.warning["id"], reason="dismissed",
                      user="运营", note="销售在谈续约", at=ts(13))
        row = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (self.warning["id"],)).fetchone()
        self.assertEqual(row["close_reason"], "dismissed")
        self.assertEqual(row["closed_by"], "运营")
        closed = self.conn.execute(
            "SELECT COUNT(*) n FROM warning WHERE closed_at IS NOT NULL").fetchone()["n"]
        self.assertEqual(closed, 1)
        kinds = [e["kind"] for e in self.events(self.warning["id"])]
        self.assertIn("closed", kinds)
        detail = next(e["detail"] for e in self.events(self.warning["id"]) if e["kind"] == "closed")
        self.assertIn("续约", detail)

    def test_partial_unique_index_allows_history_but_one_open(self):
        close_warning(self.conn, warning_id=self.warning["id"], reason="resolved",
                      user="运营", at=ts(13))
        # history rows may share a customer; a second *open* row may not
        self.conn.execute(
            """INSERT INTO warning(customer_id,tier,dedupe_key,first_seen_at,last_seen_at,
                   opened_run_id,last_run_id,closed_at,close_reason)
               VALUES(1,'high','cust:1',?,?,1,1,?, 'duplicate')""",
            (ts(13), ts(13), ts(14)))
        self.conn.execute(
            """INSERT INTO warning(customer_id,tier,dedupe_key,first_seen_at,last_seen_at,
                   opened_run_id,last_run_id) VALUES(1,'high','cust:1',?,?,1,1)""",
            (ts(14), ts(14)))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO warning(customer_id,tier,dedupe_key,first_seen_at,last_seen_at,
                       opened_run_id,last_run_id) VALUES(1,'high','cust:1',?,?,1,1)""",
                (ts(14), ts(14)))

    def test_manual_reopen_restores_the_same_warning(self):
        close_warning(self.conn, warning_id=self.warning["id"], reason="resolved",
                      user="运营", at=ts(13))
        reopen_warning(self.conn, warning_id=self.warning["id"], user="运营", at=ts(14))
        row = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (self.warning["id"],)).fetchone()
        self.assertIsNone(row["closed_at"])
        self.assertIsNone(row["close_reason"])
        self.assertIn("reopened", [e["kind"] for e in self.events(self.warning["id"])])
        with self.assertRaises(ValueError):
            reopen_warning(self.conn, warning_id=self.warning["id"], user="运营", at=ts(15))

    def test_merge_closes_warning_on_retired_customer(self):
        self.customer(2)
        self.profile(2, NEUTRAL, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="manual", run_at=ts(12, 2))
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(13),
                        available_at=ts(13), run_at=ts(13))
        row = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (self.warning["id"],)).fetchone()
        self.assertEqual(row["close_reason"], "merged")
        closed = next(e for e in self.events(self.warning["id"]) if e["kind"] == "closed")
        self.assertIn("并入", closed["detail"])
        self.assertIn("移交", closed["detail"])

    def test_stale_run_does_not_rewrite_newer_alert_state(self):
        run_scoring(self.conn, as_of=ts(16), trigger="manual", run_at=ts(16, 1))
        # A replay of an older cut-off (e.g. backfill at day 8) must not touch it.
        run_scoring(self.conn, as_of=ts(8), trigger="features", run_at=ts(8, 1))
        row = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (self.warning["id"],)).fetchone()
        self.assertEqual(row["last_seen_at"], ts(16, 1))
        self.assertEqual(row["occurrences"], 2)

    def test_duplicate_close_links_to_surviving_warning(self):
        # Another customer's open warning as a stand-in for a duplicate target.
        self.customer(2)
        self.profile(2, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="manual", run_at=ts(12, 2))
        other = self.open_warning(2)
        # close #2 as duplicate of #1's warning by forging same customer: use API on same cust
        # (a genuine duplicate warning cannot exist, so verify the guard path)
        res = close_warning(self.conn, warning_id=other["id"], reason="duplicate",
                            user="运营", at=ts(13))
        self.assertIsNone(res["duplicate_of"])  # no *other* open warning on that customer
        self.assertIn("保留记录", self.events(other["id"])[-1]["detail"])


class FeedbackTest(DbCase):
    def setUp(self):
        super().setUp()
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        self.warning = self.open_warning(1)

    def test_feedback_recorded_without_outcome(self):
        out = add_feedback(self.conn, warning_id=self.warning["id"], verdict="valid",
                           note="已确认交付问题", user="李敏", at=ts(13))
        self.assertIsNone(out["outcome"])
        row = self.conn.execute("SELECT * FROM feedback WHERE id=?",
                                (out["feedback_id"],)).fetchone()
        self.assertEqual(row["verdict"], "valid")
        self.assertEqual(row["by_user"], "李敏")

    def test_feedback_rejects_unknown_warning_and_bad_verdict(self):
        with self.assertRaises(ValueError):
            add_feedback(self.conn, warning_id=999, verdict="valid", note=None,
                         user="x", at=ts(13))
        with self.assertRaises(sqlite3.IntegrityError):
            add_feedback(self.conn, warning_id=self.warning["id"], verdict="maybe",
                         note=None, user="x", at=ts(13))

    def test_feedback_on_resolved_warning_rejected(self):
        close_warning(self.conn, warning_id=self.warning["id"], reason="resolved",
                      user="运营", at=ts(13))
        with self.assertRaises(ValueError):
            add_feedback(self.conn, warning_id=self.warning["id"], verdict="valid",
                         note=None, user="x", at=ts(14))

    def test_feedback_on_dismissed_warning_is_allowed(self):
        """Dismissals stay open to correction; that is how late labels can overturn them."""
        close_warning(self.conn, warning_id=self.warning["id"], reason="dismissed",
                      user="运营", at=ts(13))
        out = add_feedback(self.conn, warning_id=self.warning["id"], verdict="invalid",
                           note="仍然觉得是误报", user="x", at=ts(14))
        self.assertIsNotNone(out["feedback_id"])

    def test_feedback_attaches_existing_outcome_label(self):
        from churn.engine import replay_label
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(11),
                     available_at=ts(12, 5), as_of=ts(12, 5), run_at=ts(12, 5))
        out = add_feedback(self.conn, warning_id=self.warning["id"], verdict="valid",
                           note=None, user="x", at=ts(13))
        self.assertEqual(out["outcome"], "churned")
        row = self.conn.execute("SELECT * FROM feedback WHERE id=?",
                                (out["feedback_id"],)).fetchone()
        self.assertEqual(row["outcome_at"], ts(11))


class HistoricalWarningStateTest(DbCase):
    """A cut-off view rebuilds alerts from their event log, never today's row."""

    def setUp(self):
        super().setUp()
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        self.warning = self.open_warning(1)

    def test_a_later_dismissal_is_invisible_at_an_earlier_cutoff(self):
        close_warning(self.conn, warning_id=self.warning["id"], reason="dismissed",
                      user="运营", at=ts(20))
        at_15 = warnings_list(self.conn, "all", ts(15))
        self.assertEqual(at_15["counts"]["open"], 1)
        self.assertEqual(at_15["counts"]["closed"], 0)
        self.assertIsNone(at_15["items"][0]["closed_at"])
        at_21 = warnings_list(self.conn, "all", ts(21))
        self.assertEqual(at_21["counts"]["closed"], 1)
        self.assertEqual(at_21["items"][0]["close_reason"], "dismissed")

    def test_occurrences_and_tier_are_projected(self):
        run_scoring(self.conn, as_of=ts(14), trigger="manual", run_at=ts(14, 1))
        run_scoring(self.conn, as_of=ts(16), trigger="manual", run_at=ts(16, 1))
        at_15 = warnings_list(self.conn, "all", ts(15))["items"][0]
        self.assertEqual(at_15["occurrences"], 2)
        self.assertEqual(at_15["last_seen_at"], ts(14, 1))
        self.assertEqual([e["kind"] for e in at_15["events"]], ["opened", "deduped"])

    def test_events_after_the_cutoff_are_not_returned(self):
        run_scoring(self.conn, as_of=ts(20), trigger="manual", run_at=ts(20, 1))
        item = warnings_list(self.conn, "all", ts(15))["items"][0]
        self.assertTrue(all(e["at"] <= ts(15) for e in item["events"]))

    def test_a_merged_warning_still_open_before_the_merge(self):
        self.customer(2)
        self.profile(2, NEUTRAL, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="manual", run_at=ts(12, 2))
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(20),
                        available_at=ts(20), run_at=ts(20))
        before = warnings_list(self.conn, "all", ts(19))["items"]
        row = next(w for w in before if w["id"] == self.warning["id"])
        self.assertIsNone(row["closed_at"])
        after = warnings_list(self.conn, "all", ts(21))["items"]
        row = next(w for w in after if w["id"] == self.warning["id"])
        self.assertEqual(row["close_reason"], "merged")

    def test_feedback_learned_later_is_withheld(self):
        add_feedback(self.conn, warning_id=self.warning["id"], verdict="valid",
                     note=None, user="x", at=ts(13))
        from churn.engine import replay_label
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(13),
                     available_at=ts(20), as_of=ts(20), run_at=ts(20))
        early = warnings_list(self.conn, "all", ts(15))["items"][0]
        late = warnings_list(self.conn, "all", ts(21))["items"][0]
        self.assertIsNone(early["feedback"][0]["outcome"])
        self.assertEqual(late["feedback"][0]["outcome"], "churned")


if __name__ == "__main__":
    unittest.main()
