"""SQLite storage (proposal §9). Raw readings are immutable: corrections
insert new human_verified rows linked by supersedes/superseded_by, and
every decision lands in the append-only audit_log."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id     TEXT NOT NULL,
  meter_id      TEXT NOT NULL,
  register      TEXT NOT NULL,
  ts            TEXT NOT NULL,              -- ISO 8601 with offset
  value         REAL NOT NULL,
  unit          TEXT,
  confidence    REAL,
  status        TEXT NOT NULL,              -- auto | needs_review | human_verified
  flag_reason   TEXT,                       -- why it needs review
  resolution    TEXT,                       -- pending | confirmed | corrected | rejected
  capture_round TEXT,
  supersedes    INTEGER REFERENCES readings(id),
  superseded_by INTEGER REFERENCES readings(id),
  reviewed_by   TEXT,
  reviewed_at   TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (device_id, register, ts, status)
);
CREATE INDEX IF NOT EXISTS ix_readings_series
  ON readings (meter_id, register, ts);
CREATE INDEX IF NOT EXISTS ix_readings_round
  ON readings (capture_round);

CREATE TABLE IF NOT EXISTS alerts (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  rule      TEXT NOT NULL,
  meter_id  TEXT NOT NULL,
  severity  TEXT NOT NULL,                  -- critical | warning
  fired_on  TEXT NOT NULL,                  -- dedupe key: one per rule/meter/day
  fired_at  TEXT NOT NULL,
  message   TEXT NOT NULL,
  state     TEXT NOT NULL DEFAULT 'active',
  UNIQUE (rule, meter_id, fired_on)
);

CREATE TABLE IF NOT EXISTS audit_log (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  actor      TEXT NOT NULL,
  action     TEXT NOT NULL,
  entity_ref TEXT,
  before     TEXT,
  after      TEXT
);
"""

ACCEPTED = ("auto", "human_verified")


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(conn, actor: str, action: str, entity_ref: str = None,
          before=None, after=None) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, entity_ref, before, after) "
        "VALUES (?,?,?,?,?,?)",
        (utcnow(), actor, action, entity_ref,
         json.dumps(before) if before is not None else None,
         json.dumps(after) if after is not None else None),
    )


def accepted_series(conn, meter_id: str, register: str):
    """Sorted (ts, value) of accepted, non-superseded readings — the only
    data the engine ever computes from (§7.3, §9.3)."""
    rows = conn.execute(
        "SELECT ts, value FROM readings "
        "WHERE meter_id=? AND register=? AND status IN (?,?) "
        "AND superseded_by IS NULL ORDER BY ts",
        (meter_id, register, *ACCEPTED),
    ).fetchall()
    return [(r["ts"], r["value"]) for r in rows]


def last_accepted_before(conn, meter_id: str, register: str, ts: str):
    row = conn.execute(
        "SELECT ts, value FROM readings "
        "WHERE meter_id=? AND register=? AND ts<? AND status IN (?,?) "
        "AND superseded_by IS NULL ORDER BY ts DESC LIMIT 1",
        (meter_id, register, ts, *ACCEPTED),
    ).fetchone()
    return (row["ts"], row["value"]) if row else None


def meter_stats(conn, meter_id: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(ts) AS last_ts FROM readings WHERE meter_id=?",
        (meter_id,),
    ).fetchone()
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE meter_id=? AND status='needs_review' "
        "AND (resolution IS NULL OR resolution='pending')",
        (meter_id,),
    ).fetchone()
    return {"readings": row["n"], "last_ts": row["last_ts"],
            "review_pending": pending["n"]}
