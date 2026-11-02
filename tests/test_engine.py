"""Engine tests. Examples A/B/C pin the worked examples from proposal §2.5,
whose arithmetic traces to the MSEDCL 2018 methodology and Circular 312."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pfmon.engine import (  # noqa: E402
    capacitor_kvar, classify, compute_pf, cycle_start, daily_deltas,
    summarize, unwrap_delta, window_delta,
)

RULES = {"penalty_below": 0.90, "incentive_above": 0.95,
         "no_incentive_if_lead_dominant": True}
IST = timezone(timedelta(hours=5, minutes=30))


class TestWorkedExamples(unittest.TestCase):
    def test_example_a_incentive(self):
        pf, nature = compute_pf(100000, 28000, 1500)
        self.assertAlmostEqual(pf, 0.959, places=3)
        self.assertEqual(nature, "lag")
        self.assertEqual(classify(pf, nature, RULES), "INCENTIVE")

    def test_example_b_penalty(self):
        pf, nature = compute_pf(100000, 52000, 0)
        self.assertAlmostEqual(pf, 0.887, places=3)
        self.assertEqual(classify(pf, nature, RULES), "PENALTY")

    def test_example_c_lead_dominant_no_incentive(self):
        pf, nature = compute_pf(100000, 2000, 30000)
        self.assertAlmostEqual(pf, 0.952, places=3)
        self.assertEqual(nature, "lead")
        # PF > 0.95 but lead-dominant: Circular 312 disallows the incentive
        self.assertEqual(classify(pf, nature, RULES), "NEUTRAL")

    def test_penalty_applies_lead_or_lag(self):
        pf, nature = compute_pf(100000, 0, 60000)
        self.assertEqual(nature, "lead")
        self.assertEqual(classify(pf, nature, RULES), "PENALTY")

    def test_zero_energy(self):
        pf, _ = compute_pf(0, 0, 0)
        self.assertIsNone(pf)
        self.assertEqual(classify(pf, "lag", RULES), "NO_DATA")


class TestDeltas(unittest.TestCase):
    def test_plain_delta(self):
        self.assertEqual(unwrap_delta(100.0, 125.5, 1000000), 25.5)

    def test_rollover_unwrap(self):
        # 999990 -> 15 on a 1,000,000 full-scale display = +25
        self.assertAlmostEqual(unwrap_delta(999990, 15, 1000000), 25.0)

    def test_negative_without_rollover_stays_negative(self):
        # mid-scale decrease is NOT a rollover; pipeline flags it
        self.assertEqual(unwrap_delta(500000, 499000, 1000000), -1000)

    def test_daily_deltas(self):
        series = [("2026-10-01T10:00:00+05:30", 100.0),
                  ("2026-10-01T23:45:00+05:30", 110.0),
                  ("2026-10-02T23:45:00+05:30", 135.0),
                  ("2026-10-03T23:45:00+05:30", 150.0)]
        dd = daily_deltas(series, 1000000)
        self.assertEqual(dd["2026-10-02"], 25.0)
        self.assertEqual(dd["2026-10-03"], 15.0)
        self.assertNotIn("2026-10-01", dd)  # no prior day to diff against

    def test_window_delta_baseline_before_window(self):
        series = [("2026-09-30T23:45:00+05:30", 90.0),
                  ("2026-10-01T12:00:00+05:30", 100.0),
                  ("2026-10-15T12:00:00+05:30", 160.0)]
        d = window_delta(series, "2026-10-01", "2026-11-01", 1000000)
        self.assertEqual(d, 70.0)  # anchored at the last pre-cycle reading


class TestCycles(unittest.TestCase):
    def test_cycle_start_same_month(self):
        self.assertEqual(cycle_start(datetime(2026, 11, 2).date(), 1).isoformat(),
                         "2026-11-01")

    def test_cycle_start_previous_month(self):
        self.assertEqual(cycle_start(datetime(2026, 11, 2).date(), 5).isoformat(),
                         "2026-10-05")


class TestCapacitor(unittest.TestCase):
    def test_proposal_example_145_kvar(self):
        # §8.5: 500 kW from PF 0.88 to 0.97 ≈ 145 kVAr
        self.assertAlmostEqual(capacitor_kvar(500, 0.88, 0.97), 145, delta=1)

    def test_rejects_non_improvement(self):
        with self.assertRaises(ValueError):
            capacitor_kvar(500, 0.97, 0.88)


class TestSummarize(unittest.TestCase):
    def _series(self, tan_lag, tan_lead, days=20, kw=400):
        """Synthetic cumulative registers at 1 reading/day."""
        import math
        start = datetime(2026, 10, 20, 0, 0, tzinfo=IST)
        regs = {"kwh": 1000.0, "rkvah_lag": 200.0, "rkvah_lead": 10.0,
                "kvah": 1100.0}
        series = {r: [] for r in regs}
        for i in range(days * 24):
            t = start + timedelta(hours=i)
            dk = kw
            regs["kwh"] += dk
            regs["rkvah_lag"] += dk * tan_lag
            regs["rkvah_lead"] += dk * tan_lead
            regs["kvah"] += math.sqrt(dk ** 2 + (dk * (tan_lag + tan_lead)) ** 2)
            for r in regs:
                series[r].append((t.isoformat(), regs[r]))
        return series, start + timedelta(days=days)

    def test_penalty_meter_summary_and_projection(self):
        series, now = self._series(0.52, 0.0)
        s = summarize(series, {"register_fullscale": 10**6, "cycle_anchor_day": 1},
                      {"pf_rules": RULES, "energy_rate_inr_per_kvah": 8.5}, now)
        self.assertEqual(s["cycle"]["band"], "PENALTY")
        self.assertAlmostEqual(s["cycle"]["pf"], 0.887, places=2)
        self.assertEqual(s["projection"]["band"], "PENALTY")
        self.assertGreater(s["cycle"]["excess_cost_inr"], 0)

    def test_lead_dominant_summary(self):
        series, now = self._series(0.04, 0.25)
        s = summarize(series, {"register_fullscale": 10**6, "cycle_anchor_day": 1},
                      {"pf_rules": RULES, "energy_rate_inr_per_kvah": 8.5}, now)
        self.assertEqual(s["cycle"]["nature"], "lead")
        self.assertGreater(s["cycle"]["pf"], 0.95)
        self.assertEqual(s["cycle"]["band"], "NEUTRAL")  # no incentive


if __name__ == "__main__":
    unittest.main()
