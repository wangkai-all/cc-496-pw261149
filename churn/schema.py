"""SQLite schema for the churn-warning app.

Design notes
------------
Everything that can be revised is versioned rather than overwritten:

* ``feature_version`` – append-only feature revisions per customer. A revision
  carries its own ``effective_at`` (business time of the underlying data) and
  ``available_at`` (when the platform learned it). The gap is the "data time"
  we surface in the UI; a revision whose ``available_at`` is after the scoring
  cut-off is *late arriving*.
* ``customer_merge`` – aliases map a retired id onto its survivor. No feature
  or score rows are deleted on merge; scoring resolves ids at the moment of the
  run and records which entity it scored.
* ``label_event`` – churn labels are append-only with a version; a backdated
  replay creates a new version and never mutates history.
* ``feature_set`` / ``scoring_run`` / ``customer_score`` – a scoring run pins the
  exact feature-set version and, per customer, the exact feature revision used.
  Scores are immutable; re-scoring affected customers creates a new run.
* ``warning`` – one *open* warning per customer; the dedupe key is stable and
  ``closed_at`` + reason make it terminal. Re-opening is a new warning pointing
  back through ``supersedes`` so history is not rewritten.
* ``feedback`` – per warning; a label that arrives later (even backdated) links
  back and can overturn a dismissal without deleting it.
* ``job_run`` – audit of recompute campaigns, affected-id lists and timings.
"""

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS customer (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    region          TEXT NOT NULL,
    segment         TEXT NOT NULL,
    merged_into     INTEGER REFERENCES customer(id),
    merged_at       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_def (
    code            TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    unit            TEXT NOT NULL DEFAULT '',
    direction       TEXT NOT NULL CHECK (direction IN ('higher_risk','lower_risk')),
    description     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_set (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    notes           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feature_set_member (
    set_version     INTEGER NOT NULL REFERENCES feature_set(version),
    feature_code    TEXT NOT NULL REFERENCES feature_def(code),
    weight          REAL NOT NULL,
    PRIMARY KEY (set_version, feature_code)
);

-- Append-only feature revisions. status: active | superseded
CREATE TABLE IF NOT EXISTS feature_version (
    id              INTEGER PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customer(id),
    feature_code    TEXT NOT NULL REFERENCES feature_def(code),
    value           REAL,
    effective_at    TEXT NOT NULL,          -- business time of the data
    available_at    TEXT NOT NULL,          -- when we could have seen it
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','superseded')),
    -- why value is NULL: not_collected | source_outage | below_maturity |
    -- opt_out | not_applicable | pending_backfill
    missing_reason  TEXT,
    source          TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL,
    replaces_id     INTEGER REFERENCES feature_version(id)
);
CREATE INDEX IF NOT EXISTS idx_fv_customer ON feature_version(customer_id, feature_code, status);
CREATE INDEX IF NOT EXISTS idx_fv_available ON feature_version(available_at);

CREATE TABLE IF NOT EXISTS customer_merge (
    id              INTEGER PRIMARY KEY,
    from_customer   INTEGER NOT NULL REFERENCES customer(id),
    to_customer     INTEGER NOT NULL REFERENCES customer(id),
    effective_at    TEXT NOT NULL,
    available_at    TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    undone_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_merge_from ON customer_merge(from_customer);
CREATE INDEX IF NOT EXISTS idx_merge_to ON customer_merge(to_customer);

CREATE TABLE IF NOT EXISTS label_event (
    id              INTEGER PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customer(id),
    label_version   INTEGER NOT NULL,
    value           TEXT NOT NULL CHECK (value IN ('churned','retained','unknown')),
    effective_at    TEXT NOT NULL,          -- when the outcome actually happened
    available_at    TEXT NOT NULL,          -- when we learned about it
    backdated       INTEGER NOT NULL DEFAULT 0,
    retracted       INTEGER NOT NULL DEFAULT 0,
    retracts        INTEGER REFERENCES label_event(id),  -- version withdrawn by this row
    source          TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_label_customer ON label_event(customer_id, label_version);

CREATE TABLE IF NOT EXISTS scoring_run (
    id              INTEGER PRIMARY KEY,
    run_at          TEXT NOT NULL,          -- platform time the run started
    as_of           TEXT NOT NULL,          -- data cut-off; revisions after this are late
    feature_set_version INTEGER NOT NULL REFERENCES feature_set(version),
    trigger         TEXT NOT NULL,          -- initial | features | merge | label | manual
    trigger_ref     TEXT,
    scope           TEXT NOT NULL,          -- full | affected
    scored_count    INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    watermark       TEXT,                   -- latest available_at considered
    notes           TEXT
);

-- One row per scored customer per run. Immutable.
CREATE TABLE IF NOT EXISTS customer_score (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES scoring_run(id),
    customer_id     INTEGER NOT NULL REFERENCES customer(id),
    entity_id       INTEGER NOT NULL,       -- resolved after merges
    score           REAL NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('critical','high','medium','low')),
    prev_tier       TEXT,
    tier_changed    INTEGER NOT NULL DEFAULT 0,
    evidence        TEXT NOT NULL,          -- JSON: per-feature contributions
    missing         TEXT NOT NULL,          -- JSON: feature -> {reason, stale_days}
    stale_features  INTEGER NOT NULL DEFAULT 0,
    late_features   INTEGER NOT NULL DEFAULT 0,
    feature_set_version INTEGER NOT NULL,
    UNIQUE (run_id, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_score_customer ON customer_score(customer_id);
CREATE INDEX IF NOT EXISTS idx_score_run ON customer_score(run_id);

-- Open/closed alerts. One *open* warning per customer (partial unique index);
-- closed rows keep their dedupe_key so history stays queryable.
CREATE TABLE IF NOT EXISTS warning (
    id              INTEGER PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customer(id),
    tier            TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    occurrences     INTEGER NOT NULL DEFAULT 1,
    opened_run_id   INTEGER NOT NULL REFERENCES scoring_run(id),
    last_run_id     INTEGER NOT NULL REFERENCES scoring_run(id),
    closed_at       TEXT,
    close_reason    TEXT,                   -- dismissed | recovered | merged | superseded | retracted | outcome_confirmed
    closed_by       TEXT,
    supersedes      INTEGER REFERENCES warning(id),
    score_delta     REAL NOT NULL DEFAULT 0,
    opening_score   REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_warning_open
    ON warning(customer_id) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_warning_customer ON warning(customer_id);

CREATE TABLE IF NOT EXISTS warning_event (
    id              INTEGER PRIMARY KEY,
    warning_id      INTEGER NOT NULL REFERENCES warning(id),
    at              TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- opened | refreshed | closed | reopened | deduped
    detail          TEXT NOT NULL,
    run_id          INTEGER REFERENCES scoring_run(id),
    -- Structured copies so a historical view can rebuild the alert state at a
    -- cut-off from the event log instead of reading today's row.
    tier            TEXT,
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY,
    warning_id      INTEGER NOT NULL REFERENCES warning(id),
    customer_id     INTEGER NOT NULL REFERENCES customer(id),
    verdict         TEXT NOT NULL CHECK (verdict IN ('valid','invalid','unsure')),
    note            TEXT,
    by_user         TEXT NOT NULL,
    at              TEXT NOT NULL,
    -- set when a later (possibly backdated) label confirms / contradicts verdict
    outcome         TEXT,                   -- churned | retained
    outcome_at      TEXT,
    outcome_label_id INTEGER REFERENCES label_event(id),
    outcome_note    TEXT,
    outcome_retracted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedback_warning ON feedback(warning_id);

CREATE TABLE IF NOT EXISTS job_run (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,          -- backfill | merge | label_replay | rescore
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    affected_json   TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    note            TEXT
);
"""
