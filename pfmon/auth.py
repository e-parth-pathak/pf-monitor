"""Authentication & roles — stdlib only.

Two roles:
  admin  — the application owner ("super admin"): can do everything,
           including running the simulator and adjudicating the review queue.
  viewer — the end user: read-only access to dashboards, alerts,
           review queue and configuration.

Passwords are stored as PBKDF2-HMAC-SHA256 (salted, 240k iterations).
Sessions are random 256-bit tokens in the database with a fixed TTL,
sent to the browser as an HttpOnly cookie. All role checks happen
server-side in serve.py; the UI only hides what the server refuses.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username   TEXT PRIMARY KEY,
  role       TEXT NOT NULL CHECK (role IN ('admin','viewer')),
  pw_salt    BLOB NOT NULL,
  pw_hash    BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  username   TEXT NOT NULL REFERENCES users(username),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
"""

PBKDF2_ITERATIONS = 240_000
SESSION_HOURS = 12

# Demo-only seed credentials. Change before any non-demo exposure.
DEFAULT_USERS = (
    ("admin", "admin123", "admin"),   # super admin / application owner
    ("user", "user123", "viewer"),    # end user
)


def init(conn) -> None:
    conn.executescript(SCHEMA)


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                               PBKDF2_ITERATIONS)


def create_user(conn, username: str, password: str, role: str) -> None:
    salt = secrets.token_bytes(16)
    conn.execute(
        "INSERT INTO users (username, role, pw_salt, pw_hash, created_at) "
        "VALUES (?,?,?,?,?)",
        (username, role, salt, _hash(password, salt), db.utcnow()),
    )
    db.audit(conn, "system", "user_created", f"user:{username}",
             after={"role": role})


def seed_defaults(conn) -> list:
    """Create the two demo accounts if the users table is empty.
    Returns the (username, password, role) tuples it created."""
    n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if n:
        return []
    for username, password, role in DEFAULT_USERS:
        create_user(conn, username, password, role)
    conn.commit()
    return list(DEFAULT_USERS)


def verify(conn, username: str, password: str):
    """Returns {'username','role'} on success, else None."""
    row = conn.execute("SELECT * FROM users WHERE username=?",
                       (username,)).fetchone()
    if row is None:
        _hash(password, b"timing-equalizer")  # same cost for unknown users
        return None
    if not hmac.compare_digest(_hash(password, row["pw_salt"]), row["pw_hash"]):
        return None
    return {"username": row["username"], "role": row["role"]}


# ------------------------------------------------------------------ sessions

def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(conn, username: str) -> str:
    token = secrets.token_hex(32)
    now = _now()
    conn.execute(
        "INSERT INTO sessions (token, username, created_at, expires_at) "
        "VALUES (?,?,?,?)",
        (token, username, now.isoformat(timespec="seconds"),
         (now + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")),
    )
    conn.commit()
    return token


def session_user(conn, token: str):
    """Returns {'username','role'} for a live session, else None."""
    if not token:
        return None
    row = conn.execute(
        "SELECT s.token, s.expires_at, u.username, u.role "
        "FROM sessions s JOIN users u ON u.username = s.username "
        "WHERE s.token=?", (token,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now().isoformat(timespec="seconds"):
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    return {"username": row["username"], "role": row["role"]}


def destroy_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
