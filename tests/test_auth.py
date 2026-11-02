"""Authentication & session tests."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pfmon import auth, db  # noqa: E402


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.db"))
        auth.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_seed_creates_two_roles_once(self):
        created = auth.seed_defaults(self.conn)
        self.assertEqual({(u, r) for u, _, r in created},
                         {("admin", "admin"), ("user", "viewer")})
        self.assertEqual(auth.seed_defaults(self.conn), [])  # idempotent

    def test_verify_correct_and_wrong_password(self):
        auth.seed_defaults(self.conn)
        self.assertEqual(auth.verify(self.conn, "admin", "admin123"),
                         {"username": "admin", "role": "admin"})
        self.assertIsNone(auth.verify(self.conn, "admin", "wrong"))
        self.assertIsNone(auth.verify(self.conn, "ghost", "admin123"))

    def test_password_storage_is_salted_hash(self):
        auth.seed_defaults(self.conn)
        row = self.conn.execute(
            "SELECT pw_salt, pw_hash FROM users WHERE username='admin'").fetchone()
        self.assertNotIn(b"admin123", row["pw_hash"])
        self.assertEqual(len(row["pw_salt"]), 16)

    def test_session_roundtrip_and_logout(self):
        auth.seed_defaults(self.conn)
        token = auth.create_session(self.conn, "user")
        self.assertEqual(auth.session_user(self.conn, token),
                         {"username": "user", "role": "viewer"})
        auth.destroy_session(self.conn, token)
        self.assertIsNone(auth.session_user(self.conn, token))

    def test_expired_session_is_rejected_and_purged(self):
        auth.seed_defaults(self.conn)
        token = auth.create_session(self.conn, "admin")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat(timespec="seconds")
        self.conn.execute("UPDATE sessions SET expires_at=? WHERE token=?",
                          (past, token))
        self.assertIsNone(auth.session_user(self.conn, token))
        self.assertIsNone(self.conn.execute(
            "SELECT token FROM sessions WHERE token=?", (token,)).fetchone())

    def test_garbage_token_rejected(self):
        self.assertIsNone(auth.session_user(self.conn, ""))
        self.assertIsNone(auth.session_user(self.conn, "not-a-token"))


if __name__ == "__main__":
    unittest.main()
