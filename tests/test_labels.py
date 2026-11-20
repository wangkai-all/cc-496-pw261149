"""Late-arriving labels, backdating, retraction and the replay policies."""

from __future__ import annotations

import unittest

from churn.engine import (
    add_feedback,
    close_warning,
    replay_label,
    run_scoring,
)
from churn.queries import validation
from tests.support import RISKY, DbCase, ts


class LabelVersioningTest(DbCase):
    def setUp(self):
        super().setUp()
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        self.warning = self.open_warning(1)

    def test_label_versions_are_append_only(self):
        first = replay_label(self.conn, customer_id=1, value="retained",
                             effective_at=ts(11), available_at=ts(13),
                             as_of=ts(13), run_at=ts(13))
        second = replay_label(self.conn, customer_id=1, value="churned",
                              effective_at=ts(12), available_at=ts(20),
                              backdated=True, as_of=ts(20), run_at=ts(20))
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        rows = self.conn.execute(
            "SELECT * FROM label_event ORDER BY label_version").fetchall()
        self.assertEqual([r["value"] for r in rows], ["retained", "churned"])
        self.assertEqual(rows[0]["retracted"], 0)
        self.assertEqual(rows[1]["backdated"], 1)

    def test_delay_days_are_derived_from_the_two_clocks(self):
        job = replay_label(self.conn, customer_id=1, value="churned",
                           effective_at=ts(60), available_at=ts(66),
                           as_of=ts(66), run_at=ts(66))
        row = self.conn.execute("SELECT * FROM label_event WHERE id=?",
                                (job["label_id"],)).fetchone()
        self.assertEqual(row["effective_at"], ts(60))
        self.assertEqual(row["available_at"], ts(66))
        self.assertEqual(row["backdated"], 1)

    def test_late_label_does_not_create_lookahead_for_its_warning(self):
        """The scoring history is not rewritten by a label that arrives later."""
        run_before = [dict(r) for r in self.conn.execute(
            "SELECT * FROM customer_score WHERE run_id=1 ORDER BY id")]
        runs_before = self.conn.execute("SELECT COUNT(*) n FROM scoring_run").fetchone()["n"]
        replay_label(self.conn, customer_id=1, value="churned",
                     effective_at=ts(30), available_at=ts(40),
                     as_of=ts(40), run_at=ts(40))
        run_after = [dict(r) for r in self.conn.execute(
            "SELECT * FROM customer_score WHERE run_id=1 ORDER BY id")]
        self.assertEqual(run_before, run_after)  # the original run is untouched
        # the replay creates a *new* run rather than rewriting older scores
        runs_after = self.conn.execute("SELECT COUNT(*) n FROM scoring_run").fetchone()["n"]
        self.assertEqual(runs_after, runs_before + 1)


class RetractionTest(DbCase):
    def setUp(self):
        super().setUp()
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        self.warning = self.open_warning(1)
        add_feedback(self.conn, warning_id=self.warning["id"], verdict="valid",
                     note=None, user="x", at=ts(13))

    def test_retraction_flags_the_old_version_and_appends_a_v3(self):
        first = replay_label(self.conn, customer_id=1, value="churned",
                             effective_at=ts(11), available_at=ts(14),
                             as_of=ts(14), run_at=ts(14))
        retract = replay_label(self.conn, customer_id=1, value="unknown",
                               effective_at=ts(11), available_at=ts(15),
                               retract=True, as_of=ts(15), run_at=ts(15))
        self.assertEqual(retract["version"], 2)
        rows = self.conn.execute(
            "SELECT * FROM label_event ORDER BY label_version").fetchall()
        self.assertEqual(rows[0]["value"], "churned")
        self.assertEqual(rows[0]["retracted"], 1)      # the withdrawn claim is flagged
        self.assertEqual(rows[1]["value"], "unknown")
        self.assertEqual(rows[1]["retracted"], 0)      # the retraction itself stands
        self.assertEqual(first["label_id"], rows[0]["id"])

    def test_retraction_keeps_feedback_but_marks_the_outcome_withdrawn(self):
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(13),
                     available_at=ts(14), as_of=ts(14), run_at=ts(14))
        fb = self.conn.execute("SELECT * FROM feedback").fetchone()
        self.assertEqual(fb["outcome"], "churned")
        self.assertEqual(fb["outcome_retracted"], 0)
        replay_label(self.conn, customer_id=1, value="unknown", effective_at=ts(13),
                     available_at=ts(15), retract=True, as_of=ts(15), run_at=ts(15))
        fb = self.conn.execute("SELECT * FROM feedback").fetchone()
        self.assertEqual(fb["outcome"], "churned")     # record preserved
        self.assertEqual(fb["outcome_retracted"], 1)
        self.assertIn("撤回", fb["outcome_note"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM feedback").fetchone()["n"], 1)

    def test_retracting_an_overturning_label_restores_the_dismissal(self):
        self.customer(2)
        self.profile(2, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="manual", run_at=ts(12, 2))
        warning = self.open_warning(2)
        add_feedback(self.conn, warning_id=warning["id"], verdict="invalid",
                     note=None, user="x", at=ts(13))
        close_warning(self.conn, warning_id=warning["id"], reason="dismissed",
                      user="x", at=ts(13))
        replay_label(self.conn, customer_id=2, value="churned", effective_at=ts(20),
                     available_at=ts(25), as_of=ts(25), run_at=ts(25))
        self.assertIsNotNone(self.open_warning(2))
        replay_label(self.conn, customer_id=2, value="unknown", effective_at=ts(20),
                     available_at=ts(26), retract=True, as_of=ts(26), run_at=ts(26))
        row = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (warning["id"],)).fetchone()
        self.assertIsNotNone(row["closed_at"])
        self.assertEqual(row["close_reason"], "retracted")
        self.assertIn("撤销", self.events(warning["id"])[-1]["detail"])
        # Alerts stay score-driven: the retraction job also rescores, and a
        # customer still in an alert tier gets a *successor* alert, not the old
        # one silently reopened.
        current = self.open_warning(2)
        self.assertIsNotNone(current)
        self.assertEqual(current["supersedes"], warning["id"])

    def test_replay_policies_exclude_vs_include_retracted(self):
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(13),
                     available_at=ts(14), as_of=ts(14), run_at=ts(14))
        replay_label(self.conn, customer_id=1, value="unknown", effective_at=ts(13),
                     available_at=ts(15), retract=True, as_of=ts(15), run_at=ts(15))
        out = validation(self.conn, ts(15))
        by = {p["policy"]: p for p in out["policies"]}
        # excluded: unlabeled -> denominator shrinks to zero
        self.assertEqual(by["eventual"]["unlabeled"], 1)
        self.assertEqual(by["eventual"]["judged"], 0)
        # included: still counted as a hit, though the claim was withdrawn
        self.assertEqual(by["include_retracted"]["hit"], 1)


class ReopenFromLabelTest(DbCase):
    def test_churned_label_overturns_a_dismissal(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        warning = self.open_warning(1)
        add_feedback(self.conn, warning_id=warning["id"], verdict="invalid",
                     note="销售说在谈续约", user="王凯", at=ts(13))
        close_warning(self.conn, warning_id=warning["id"], reason="dismissed",
                      user="王凯", at=ts(13))
        out = replay_label(self.conn, customer_id=1, value="churned",
                           effective_at=ts(20), available_at=ts(25),
                           as_of=ts(25), run_at=ts(25))
        self.assertEqual(out["feedback_updated"][0]["outcome"], "churned")
        reopened = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                     (warning["id"],)).fetchone()
        self.assertIsNone(reopened["closed_at"])
        self.assertIsNone(reopened["close_reason"])
        events = [dict(e) for e in self.conn.execute(
            "SELECT * FROM warning_event WHERE warning_id=? ORDER BY id", (warning["id"],))]
        kinds = [e["kind"] for e in events]
        self.assertIn("reopened", kinds)
        reopen_event = next(e for e in events if e["kind"] == "reopened")
        self.assertEqual(reopen_event["at"], ts(25))

    def test_another_open_warning_gets_a_refresh_note_instead_of_a_second_open(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        first = self.open_warning(1)
        add_feedback(self.conn, warning_id=first["id"], verdict="invalid",
                     note=None, user="x", at=ts(13))
        close_warning(self.conn, warning_id=first["id"], reason="dismissed",
                      user="x", at=ts(13))
        # still risky at the next run -> successor warning opens
        run_scoring(self.conn, as_of=ts(14), trigger="manual", run_at=ts(14, 1))
        second = self.open_warning(1)
        self.assertNotEqual(second["id"], first["id"])
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(20),
                     available_at=ts(25), as_of=ts(25), run_at=ts(25))
        # the original warning stays closed; the overturn is noted on the open one
        old = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                (first["id"],)).fetchone()
        self.assertIsNotNone(old["closed_at"])
        events = [dict(e) for e in self.conn.execute(
            "SELECT * FROM warning_event WHERE warning_id=? ORDER BY id", (second["id"],))]
        self.assertTrue(any(e["kind"] == "refreshed" and "推翻" in e["detail"] for e in events))
        opens = self.conn.execute(
            "SELECT COUNT(*) n FROM warning WHERE customer_id=1 AND closed_at IS NULL",
        ).fetchone()["n"]
        self.assertEqual(opens, 1)

    def test_retained_label_does_not_reopen(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        warning = self.open_warning(1)
        add_feedback(self.conn, warning_id=warning["id"], verdict="invalid",
                     note=None, user="x", at=ts(13))
        close_warning(self.conn, warning_id=warning["id"], reason="dismissed",
                      user="x", at=ts(13))
        replay_label(self.conn, customer_id=1, value="retained", effective_at=ts(20),
                     available_at=ts(25), as_of=ts(25), run_at=ts(25))
        still = self.conn.execute("SELECT * FROM warning WHERE id=?",
                                  (warning["id"],)).fetchone()
        self.assertEqual(still["close_reason"], "dismissed")
        fb = self.conn.execute("SELECT * FROM feedback").fetchone()
        self.assertEqual(fb["outcome"], "retained")


class PolicyFlipTest(DbCase):
    """A late label moves the verdict of an alert only under the eventual policy."""

    def test_on_time_vs_eventual_flip(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        warning = self.open_warning(1)
        # churned on day 13, but only learned on day 60 (>30 past first_seen)
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(13),
                     available_at=ts(60), as_of=ts(60), run_at=ts(60))
        out = validation(self.conn, ts(60))
        by = {p["policy"]: p for p in out["policies"]}
        self.assertEqual(by["on_time"]["unlabeled"], 1)
        self.assertEqual(by["on_time"]["hit"], 0)
        self.assertEqual(by["eventual"]["hit"], 1)
        self.assertEqual(by["eventual"]["precision"], 1.0)
        flips = [f for f in out["flips"] if f["warning_id"] == warning["id"]]
        self.assertEqual(len(flips), 1)
        self.assertEqual((flips[0]["from"], flips[0]["to"]), ("unknown", "hit"))
        self.assertEqual(flips[0]["delay_days"], 47)

    def test_on_time_label_within_horizon_counts(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(13),
                     available_at=ts(20), as_of=ts(20), run_at=ts(20))
        by = {p["policy"]: p for p in validation(self.conn, ts(20))["policies"]}
        self.assertEqual(by["on_time"]["hit"], 1)

    def test_churn_before_warning_opened_is_a_miss(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        # churn happened before the alert opened -> alert did not predict it
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(5),
                     available_at=ts(6), as_of=ts(6), run_at=ts(6))
        by = {p["policy"]: p for p in validation(self.conn, ts(20))["policies"]}
        self.assertEqual(by["eventual"]["miss"], 1)

    def test_unknown_placeholder_never_overrides_a_real_outcome(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(13),
                     available_at=ts(14), as_of=ts(14), run_at=ts(14))
        replay_label(self.conn, customer_id=1, value="unknown", effective_at=ts(13),
                     available_at=ts(15), as_of=ts(15), run_at=ts(15))
        by = {p["policy"]: p for p in validation(self.conn, ts(15))["policies"]}
        self.assertEqual(by["eventual"]["hit"], 1)
        self.assertEqual(by["eventual"]["unlabeled"], 0)

    def test_known_at_as_of_flag(self):
        self.customer(1)
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(10),
                     available_at=ts(30), as_of=ts(30), run_at=ts(30))
        early = validation(self.conn, ts(20))
        late = validation(self.conn, ts(31))
        self.assertFalse(early["labels"][0]["known_at_as_of"])
        self.assertTrue(late["labels"][0]["known_at_as_of"])

    def test_a_cutoff_does_not_use_labels_learned_later(self):
        """`eventual` is eventual *as of the cut-off*, not as of today."""
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(13),
                     available_at=ts(20), as_of=ts(20), run_at=ts(20))
        early = {p["policy"]: p for p in validation(self.conn, ts(15))["policies"]}
        later = {p["policy"]: p for p in validation(self.conn, ts(20))["policies"]}
        self.assertEqual(early["eventual"]["unlabeled"], 1)
        self.assertEqual(later["eventual"]["hit"], 1)

    def test_retraction_is_invisible_before_it_arrived(self):
        self.customer(1)
        self.profile(1, RISKY, ts(10))
        run_scoring(self.conn, as_of=ts(12), trigger="initial", run_at=ts(12, 1))
        replay_label(self.conn, customer_id=1, value="churned", effective_at=ts(13),
                     available_at=ts(14), as_of=ts(14), run_at=ts(14))
        replay_label(self.conn, customer_id=1, value="unknown", effective_at=ts(13),
                     available_at=ts(20), retract=True, as_of=ts(20), run_at=ts(20))
        before = {p["policy"]: p for p in validation(self.conn, ts(19))["policies"]}
        after = {p["policy"]: p for p in validation(self.conn, ts(20))["policies"]}
        self.assertEqual(before["eventual"]["hit"], 1)      # label still standing
        self.assertEqual(after["eventual"]["unlabeled"], 1)  # withdrawal now known


if __name__ == "__main__":
    unittest.main()
