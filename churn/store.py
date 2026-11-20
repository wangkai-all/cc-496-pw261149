"""SQLite helpers: open a database, apply the schema once, and small utilities."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from .schema import SCHEMA


def utcnow() -> str:
    """ISO-8601 UTC timestamp, second precision, the app's canonical format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def add_days(ts: str, days: int) -> str:
    d = parse_ts(ts) + timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def days_between(later: str | None, earlier: str | None) -> int | None:
    a, b = parse_ts(later), parse_ts(earlier)
    if not a or not b:
        return None
    return int((a - b).total_seconds() // 86400)


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or os.environ.get("CHURN_DB", os.path.join(os.path.dirname(__file__), "..", "data", "churn.sqlite"))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database file was first created."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(warning_event)")}
    for name in ("tier", "reason"):
        if name not in cols:
            conn.execute(f"ALTER TABLE warning_event ADD COLUMN {name} TEXT")
    label_cols = {r["name"] for r in conn.execute("PRAGMA table_info(label_event)")}
    if "retracts" not in label_cols:
        conn.execute("ALTER TABLE label_event ADD COLUMN retracts INTEGER REFERENCES label_event(id)")


def is_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM customer").fetchone()
    return row["n"] == 0
