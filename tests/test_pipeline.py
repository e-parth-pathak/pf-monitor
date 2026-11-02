"""Ingest pipeline + review tests (proposal §7)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pfmon import db, pipeline  # noqa: E402

SETTINGS = {"confidence_threshold": 0.90, "rate_headroom": 1.5,
            "interval_minutes": 15}
METERS = {"m1": {"device_id": "d1", "sanctioned_kw": 500,
                 "register_fullscale": 1000000, "cycle_anchor_day": 1}}


def payload(register="kwh", ts="2026-11-01T10:00:00+05:30", value=1000.0,
            confidence=0.99, capture_round=None):
    return {"device_id": "d1", "meter_id": "m1", "register": register,
            "ts": ts, "value": value, "confidence": confidence,
            "capture_round": capture_round}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ingest(self, **kw):
        return pipeline.ingest(self.conn, payload(**kw), SETTINGS, METERS)

    def test_accept_and_idempotent_replay(self):
        r1 = self.ingest()
        self.assertEqual(r1["result"], "accepted")
        r2 = self.ingest()  # store-and-forward replay (§7.1)
        self.assertEqual(r2["result"], "duplicate")
        self.assertEqual(r2["id"], r1["id"])

    def test_low_confidence_flagged(self):
        r = self.ingest(confidence=0.70)
        self.assertEqual(r["result"], "flagged")
        self.assertIn("low_confidence", r["reason"])
        self.assertEqual(len(pipeline.pending_reviews(self.conn)), 1)

    def test_monotonicity_flag(self):
        self.ingest(value=5000.0)
        r = self.ingest(ts="2026-11-01T10:15:00+05:30", value=4000.0)
        self.assertEqual(r["result"], "flagged")
        self.assertIn("monotonicity", r["reason"])
        # flagged reading must NOT poison the accepted series
        self.assertEqual(db.accepted_series(self.conn, "m1", "kwh"),
                         [("2026-11-01T10:00:00+05:30", 5000.0)])

    def test_rollover_is_not_flagged(self):
        self.ingest(value=999990.0)
        r = self.ingest(ts="2026-11-01T10:15:00+05:30", value=15.0)
        self.assertEqual(r["result"], "accepted")

    def test_rate_limit_flag(self):
        self.ingest(value=1000.0)
        # +1000 kWh in 15 min => 4000 kW implied >> 500*1.5
        r = self.ingest(ts="2026-11-01T10:15:00+05:30", value=2000.0)
        self.assertEqual(r["result"], "flagged")
        self.assertIn("rate_limit", r["reason"])

    def test_cross_register_flags_whole_round(self):
        # baseline round
        base = {"kwh": 1000.0, "rkvah_lag": 270.0, "rkvah_lead": 5.0,
                "kvah": 1040.0}
        for reg, v in base.items():
            self.ingest(register=reg, value=v, capture_round="r0")
        # next round: kvah delta wildly inconsistent with the energy deltas
        nxt = {"kwh": 1100.0, "rkvah_lag": 297.0, "rkvah_lead": 5.5,
               "kvah": 1400.0}  # Δ=360 vs expected ≈ √(100²+27.5²) ≈ 103.7
        last = None
        for reg, v in nxt.items():
            last = self.ingest(register=reg, ts="2026-11-01T10:15:00+05:30",
                               value=v, capture_round="r1")
        self.assertEqual(last["result"], "accepted")  # flagged post-insert
        pending = pipeline.pending_reviews(self.conn)
        self.assertEqual(len(pending), 4)
        self.assertTrue(all("cross_register" in p["flag_reason"] for p in pending))

    def test_cross_register_skipped_on_misaligned_baselines(self):
        # round 0: all four accepted
        base = {"kwh": 1000.0, "rkvah_lag": 270.0, "rkvah_lead": 5.0,
                "kvah": 1040.0}
        for reg, v in base.items():
            self.ingest(register=reg, value=v, capture_round="r0")
        # round 1: kwh arrives low-confidence -> flagged, others accepted
        r1 = {"kwh": (1100.0, 0.60), "rkvah_lag": (297.0, 0.99),
              "rkvah_lead": (5.5, 0.99), "kvah": (1143.7, 0.99)}
        for reg, (v, conf) in r1.items():
            self.ingest(register=reg, ts="2026-11-01T10:15:00+05:30",
                        value=v, confidence=conf, capture_round="r1")
        # round 2: consistent values, but kwh baseline is now 2 intervals old
        # while the others are 1 — the check must skip, not false-flag
        r2 = {"kwh": 1200.0, "rkvah_lag": 324.0, "rkvah_lead": 6.0,
              "kvah": 1247.4}
        for reg, v in r2.items():
            self.ingest(register=reg, ts="2026-11-01T10:30:00+05:30",
                        value=v, capture_round="r2")
        pending = pipeline.pending_reviews(self.conn)
        self.assertEqual(len(pending), 1)  # only the low-confidence kwh
        self.assertIn("low_confidence", pending[0]["flag_reason"])

    def test_review_correct_supersedes(self):
        r = self.ingest(confidence=0.60, value=4321.0)
        res = pipeline.review(self.conn, r["id"], "correct", "tester",
                              corrected_value=4521.0)
        self.assertEqual(res["result"], "corrected")
        series = db.accepted_series(self.conn, "m1", "kwh")
        self.assertEqual(series, [("2026-11-01T10:00:00+05:30", 4521.0)])
        # original row kept, linked, out of the accepted series (§9.3)
        row = self.conn.execute("SELECT * FROM readings WHERE id=?",
                                (r["id"],)).fetchone()
        self.assertEqual(row["superseded_by"], res["new_id"])
        self.assertEqual(row["resolution"], "corrected")

    def test_review_reject_excludes(self):
        r = self.ingest(confidence=0.60)
        pipeline.review(self.conn, r["id"], "reject", "tester")
        self.assertEqual(db.accepted_series(self.conn, "m1", "kwh"), [])
        self.assertEqual(pipeline.pending_reviews(self.conn), [])

    def test_unknown_meter_rejected(self):
        p = payload()
        p["meter_id"] = "nope"
        r = pipeline.ingest(self.conn, p, SETTINGS, METERS)
        self.assertEqual(r["result"], "rejected")


if __name__ == "__main__":
    unittest.main()
