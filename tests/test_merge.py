"""Customer merges: entity resolution, union scoring, affected-only rescoring, undo."""

from __future__ import annotations

import unittest

from churn.engine import merge_customers, revise_feature, undo_merge
from churn.scoring import aliases_of, entity_ids, resolve_entity, score_entity
from tests.support import NEUTRAL, RISKY, DbCase, ts


class MergeResolutionTest(DbCase):
    def test_merge_applies_only_after_available_at(self):
        self.customer(1)
        self.customer(2)
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(20),
                        available_at=ts(25), run_at=ts(25))
        self.assertEqual(resolve_entity(self.conn, 1, ts(24))[0], 1)
        self.assertEqual(resolve_entity(self.conn, 1, ts(25))[0], 2)
        self.assertEqual(resolve_entity(self.conn, 2, ts(25))[0], 2)

    def test_aliases_and_reverse_walk(self):
        for cid in (1, 2, 3):
            self.customer(cid)
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(10),
                        available_at=ts(10), run_at=ts(10))
        merge_customers(self.conn, from_id=2, to_id=3, effective_at=ts(20),
                        available_at=ts(20), run_at=ts(20))
        self.assertEqual(resolve_entity(self.conn, 1, ts(15))[0], 2)
        self.assertEqual(resolve_entity(self.conn, 1, ts(21))[0], 3)
        self.assertEqual(sorted(entity_ids(self.conn, 3, ts(21))), [1, 2, 3])
        entity, ids, chain = aliases_of(self.conn, 1, ts(21))
        self.assertEqual(entity, 3)
        self.assertEqual(sorted(ids), [1, 2, 3])
        self.assertEqual([c["from"] for c in chain], [1, 2])

    def test_merge_of_same_entity_rejected(self):
        self.customer(1)
        self.customer(2)
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(10),
                        available_at=ts(10), run_at=ts(10))
        with self.assertRaises(ValueError):
            merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(11),
                            available_at=ts(11), run_at=ts(11))
        with self.assertRaises(ValueError):
            merge_customers(self.conn, from_id=2, to_id=2, effective_at=ts(11),
                            available_at=ts(11), run_at=ts(11))

    def test_score_uses_union_of_aliased_revisions(self):
        """The survivor scores over the union; the newest revision per feature wins."""
        self.customer(1)
        self.customer(2)
        self.profile(1, NEUTRAL, ts(5))
        self.profile(2, NEUTRAL, ts(5))
        self.rev(1, "invoice_overdue_days", 60.0, ts(6), ts(6))  # only the old ledger knows
        before = score_entity(self.conn, 2, ts(8))
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(9),
                        available_at=ts(9), run_at=ts(9))
        after = score_entity(self.conn, 2, ts(10))
        ev = {e["code"]: e for e in after.evidence}["invoice_overdue_days"]
        self.assertEqual(ev["value"], 60.0)
        self.assertEqual(ev["owner_id"], 1)
        self.assertGreater(after.score, before.score)

    def test_missing_in_one_ledger_is_filled_by_the_other(self):
        """A merge unions revisions; one ledger's gap must not hide the other's value."""
        self.customer(1)
        self.customer(2)
        for code in ("logins_30d", "support_tickets_30d", "seat_utilization", "nps_survey"):
            self.rev(2, code, NEUTRAL[code], ts(5))
        self.rev(1, "invoice_overdue_days", 30.0, ts(5))
        self.rev(2, "invoice_overdue_days", None, ts(6), reason="not_applicable")
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(7),
                        available_at=ts(7), run_at=ts(7))
        res = score_entity(self.conn, 2, ts(8))
        ev = {e["code"]: e for e in res.evidence}["invoice_overdue_days"]
        self.assertEqual(ev["value"], 30.0)
        self.assertEqual(ev["owner_id"], 1)
        self.assertNotIn("invoice_overdue_days", res.missing)

    def test_merge_closes_the_retired_open_warning(self):
        self.customer(1)
        self.customer(2)
        self.profile(1, RISKY, ts(5))
        self.profile(2, NEUTRAL, ts(5))
        from churn.engine import run_scoring
        run_scoring(self.conn, as_of=ts(6), trigger="initial", run_at=ts(6, 1))
        retired_warning = self.open_warning(1)
        self.assertIsNotNone(retired_warning)
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(7),
                        available_at=ts(7), run_at=ts(7))
        row = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (retired_warning["id"],)).fetchone()
        self.assertEqual(row["close_reason"], "merged")
        self.assertEqual(row["closed_at"], ts(7))
        self.assertIsNone(self.open_warning(1))

    def test_history_rows_are_not_rewritten(self):
        self.customer(1)
        self.customer(2)
        self.profile(1, RISKY, ts(5))
        self.profile(2, NEUTRAL, ts(5))
        from churn.engine import run_scoring
        run_scoring(self.conn, as_of=ts(6), trigger="initial", run_at=ts(6, 1))
        before = self.conn.execute(
            "SELECT * FROM customer_score WHERE customer_id=1").fetchall()
        merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(7),
                        available_at=ts(7), run_at=ts(7))
        after = self.conn.execute(
            "SELECT * FROM customer_score WHERE customer_id=1").fetchall()
        self.assertEqual([dict(r) for r in before], [dict(r) for r in after])

    def test_undo_merge_restores_the_split(self):
        self.customer(1)
        self.customer(2)
        self.profile(1, RISKY, ts(5))
        self.profile(2, NEUTRAL, ts(5))
        job = merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(7),
                              available_at=ts(7), run_at=ts(7))
        merge_id = job["merge_id"]
        self.assertEqual(resolve_entity(self.conn, 1, ts(8))[0], 2)
        undo_merge(self.conn, merge_id=merge_id, as_of=ts(9), run_at=ts(9))
        self.assertEqual(resolve_entity(self.conn, 1, ts(9))[0], 1)
        self.assertEqual(sorted(entity_ids(self.conn, 2, ts(9))), [2])
        customer = self.conn.execute("SELECT * FROM customer WHERE id=1").fetchone()
        self.assertIsNone(customer["merged_into"])
        with self.assertRaises(ValueError):
            undo_merge(self.conn, merge_id=merge_id, run_at=ts(10))

    def test_undo_is_itself_time_versioned(self):
        self.customer(1)
        self.customer(2)
        job = merge_customers(self.conn, from_id=1, to_id=2, effective_at=ts(7),
                              available_at=ts(7), run_at=ts(7))
        undo_merge(self.conn, merge_id=job["merge_id"], as_of=ts(9), run_at=ts(9))
        # The undo was only known on day 9: a replay of day 8 still sees the merge.
        self.assertEqual(resolve_entity(self.conn, 1, ts(8))[0], 2)
        self.assertEqual(resolve_entity(self.conn, 1, ts(9))[0], 1)
        # And a current lookup agrees with the latest known state.
        self.assertEqual(resolve_entity(self.conn, 1, ts(30))[0], 1)


class AffectedScoringTest(DbCase):
    def setUp(self):
        super().setUp()
        for cid in (1, 2, 3):
            self.customer(cid)
            self.profile(cid, NEUTRAL, ts(10))
        from churn.engine import run_scoring
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))

    def test_revise_feature_recomputes_only_that_customer(self):
        job = revise_feature(self.conn, customer_id=2, code="logins_30d", value=0.0,
                             effective_at=ts(13), run_at=ts(13, 1))
        run = job["run"]
        self.assertEqual(run["scope"], "affected")
        self.assertEqual(run["scored"], 1)
        self.assertEqual(
            [r["customer_id"] for r in self.conn.execute(
                "SELECT customer_id FROM customer_score WHERE run_id=?", (run["run_id"],))],
            [2])
        self.assertEqual(self.latest_score(1)["score"], 50.0)

    def test_revise_rejects_unknown_customer_and_feature(self):
        with self.assertRaises(Exception):
            revise_feature(self.conn, customer_id=99, code="logins_30d", value=1.0,
                           effective_at=ts(13), run_at=ts(13, 1))
        with self.assertRaises(Exception):
            revise_feature(self.conn, customer_id=1, code="nope", value=1.0,
                           effective_at=ts(13), run_at=ts(13, 1))

    def test_old_revision_is_superseded(self):
        job = revise_feature(self.conn, customer_id=2, code="logins_30d", value=1.0,
                             effective_at=ts(13), run_at=ts(13, 1))
        old = self.conn.execute(
            "SELECT * FROM feature_version WHERE id=?", (job["replaced_id"],)).fetchone()
        self.assertEqual(old["status"], "superseded")
        new = self.conn.execute("SELECT * FROM feature_version WHERE id=?",
                                (job["revision_id"],)).fetchone()
        self.assertEqual(new["status"], "active")

    def test_backdated_revision_does_not_hide_newer_one(self):
        revise_feature(self.conn, customer_id=2, code="logins_30d", value=20.0,
                       effective_at=ts(14), run_at=ts(14, 1))
        revise_feature(self.conn, customer_id=2, code="logins_30d", value=5.0,
                       effective_at=ts(13), run_at=ts(15, 1))  # backdated correction
        res = score_entity(self.conn, 2, ts(16))
        ev = {e["code"]: e for e in res.evidence}["logins_30d"]
        self.assertEqual(ev["value"], 20.0)

    def test_late_revision_is_stored_but_not_scored(self):
        job = revise_feature(self.conn, customer_id=2, code="logins_30d", value=0.0,
                             effective_at=ts(13), available_at=ts(30),
                             as_of=ts(13), run_at=ts(13, 1))
        self.assertTrue(job["late"])
        self.assertEqual(self.latest_score(2)["score"], 50.0)
        res = score_entity(self.conn, 2, ts(31))
        self.assertGreater(res.score, 50.0)

    def test_merged_id_rescores_the_survivor_not_the_retired_row(self):
        from churn.engine import merge_customers, run_scoring
        self.rev(1, "logins_30d", 0.0, ts(13))
        job = revise_feature(self.conn, customer_id=1, code="logins_30d", value=0.0,
                             effective_at=ts(13), run_at=ts(13, 1))
        merge_customers(self.conn, from_id=1, to_id=3, effective_at=ts(14),
                        available_at=ts(14), run_at=ts(14, 1))
        job = revise_feature(self.conn, customer_id=1, code="logins_30d", value=0.0,
                             effective_at=ts(15), run_at=ts(15, 1))
        self.assertEqual(job["run"]["scored"], 1)
        scored = self.conn.execute(
            "SELECT customer_id FROM customer_score WHERE run_id=?",
            (job["run"]["run_id"],)).fetchall()
        self.assertEqual([r["customer_id"] for r in scored], [3])

    def test_run_replayed_before_a_merge_scores_both_ids(self):
        """A historical cut-off scores the entity split as it was at that time."""
        from churn.engine import merge_customers, run_scoring
        merge_customers(self.conn, from_id=1, to_id=3, effective_at=ts(20),
                        available_at=ts(20), run_at=ts(20, 1))
        out = run_scoring(self.conn, as_of=ts(15), trigger="manual", run_at=ts(15, 1))
        self.assertEqual(out["scored"], 3)
        ids = {r["customer_id"] for r in self.conn.execute(
            "SELECT customer_id FROM customer_score WHERE run_id=?", (out["run_id"],))}
        self.assertEqual(ids, {1, 2, 3})

    def test_job_record_names_the_affected_set(self):
        job = revise_feature(self.conn, customer_id=2, code="logins_30d", value=1.0,
                             effective_at=ts(13), run_at=ts(13, 1))
        rec = self.conn.execute("SELECT * FROM job_run WHERE id=?", (job["job_id"],)).fetchone()
        self.assertEqual(rec["kind"], "backfill")
        self.assertIn('"revision_id"', rec["result_json"])


if __name__ == "__main__":
    unittest.main()
