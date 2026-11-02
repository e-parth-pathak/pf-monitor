"""Meter simulator — stands in for the camera/edge agents of proposal §6.

Generates 15-minute capture rounds of the five registers (§2.1) for three
demo meters and pushes every reading through the real ingest pipeline, so
the validation, review-queue and alerting paths are exercised exactly as
they would be by field devices:

  msedcl-DEMO-001  healthy lagging load, PF ≈ 0.965  -> incentive band
  msedcl-DEMO-002  heavy inductive load, PF ≈ 0.887  -> penalty band
  msedcl-DEMO-003  over-compensated, lead-dominant, PF ≈ 0.96
                   -> high PF but NO incentive (Circular 312)

A small fraction of readings is corrupted the way OCR actually fails
(dropped digit → non-monotonic value) or published with low confidence,
to populate the review queue. Deterministic under a fixed seed.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from . import pipeline

IST = timezone(timedelta(hours=5, minutes=30))

BEHAVIOUR = {
    "msedcl-DEMO-001": {"avg_kw": 350, "tan_lag": 0.27, "tan_lead": 0.005},
    "msedcl-DEMO-002": {"avg_kw": 280, "tan_lag": 0.52, "tan_lead": 0.0},
    "msedcl-DEMO-003": {"avg_kw": 180, "tan_lag": 0.04, "tan_lead": 0.25},
}


def simulate(conn, settings: dict, meters: dict, days: int = 35,
             seed: int = 42, end: datetime = None) -> dict:
    rnd = random.Random(seed)
    end = end or datetime.now(IST)
    end = end.replace(minute=(end.minute // 15) * 15, second=0, microsecond=0)
    start = end - timedelta(days=days)
    step = timedelta(minutes=settings.get("interval_minutes", 15))

    stats = {"accepted": 0, "flagged": 0, "duplicate": 0, "rejected": 0}
    for meter_id, profile in meters.items():
        beh = BEHAVIOUR.get(meter_id)
        if beh is None:
            continue
        regs = {
            "kwh": 400000.0 + rnd.random() * 50000,
            "rkvah_lag": 90000.0 + rnd.random() * 20000,
            "rkvah_lead": 4000.0 + rnd.random() * 2000,
            "kvah": 420000.0 + rnd.random() * 50000,
        }
        md_kva = 0.0
        cycle_month = None
        t = start
        while t <= end:
            # load profile: daytime plateau, night trough, ±6% noise
            hour = t.hour + t.minute / 60
            day_factor = max(0.0, min(1.0, (hour - 7) / 3)) * \
                max(0.0, min(1.0, (21 - hour) / 3))
            kw = beh["avg_kw"] * (0.55 + 0.55 * day_factor) * rnd.uniform(0.94, 1.06)
            dkwh = kw * step.total_seconds() / 3600
            dlag = dkwh * beh["tan_lag"] * rnd.uniform(0.95, 1.05)
            dlead = dkwh * beh["tan_lead"] * rnd.uniform(0.95, 1.05)
            dkvah = math.sqrt(dkwh ** 2 + (dlag + dlead) ** 2)
            regs["kwh"] += dkwh
            regs["rkvah_lag"] += dlag
            regs["rkvah_lead"] += dlead
            regs["kvah"] += dkvah

            # md_kva: cycle-maximum demand register, resets each billing month
            anchor = int(profile.get("cycle_anchor_day", 1))
            month_key = (t.year, t.month) if t.day >= anchor else \
                ((t - timedelta(days=anchor)).year, (t - timedelta(days=anchor)).month)
            if month_key != cycle_month:
                cycle_month, md_kva = month_key, 0.0
            md_kva = max(md_kva, dkvah / (step.total_seconds() / 3600))

            round_id = f"{meter_id}-{t.strftime('%Y%m%d%H%M')}"
            for reg, unit in (("kwh", "kWh"), ("rkvah_lag", "kVArh"),
                              ("rkvah_lead", "kVArh"), ("kvah", "kVAh"),
                              ("md_kva", "kVA")):
                true_value = md_kva if reg == "md_kva" else regs[reg]
                value = round(true_value, 1)
                confidence = round(rnd.uniform(0.965, 0.999), 3)
                r = rnd.random()
                if r < 0.0015 and reg != "md_kva":
                    # OCR dropped a digit: value collapses -> monotonicity flag
                    value = round(true_value - 10 ** rnd.choice((3, 4)), 1)
                    confidence = round(rnd.uniform(0.91, 0.97), 3)
                elif r < 0.0055:
                    # blurred frame: correct value but low confidence -> review
                    confidence = round(rnd.uniform(0.55, 0.88), 3)
                res = pipeline.ingest(conn, {
                    "schema": "pf.reading/1",
                    "device_id": profile.get("device_id", f"edge-{meter_id}"),
                    "meter_id": meter_id,
                    "register": reg,
                    "ts": t.isoformat(timespec="seconds"),
                    "value": value,
                    "unit": unit,
                    "confidence": confidence,
                    "capture_round": round_id,
                }, settings, meters, actor="simulator")
                key = res["result"] if res["result"] in stats else "accepted"
                stats[key] += 1
            t += step
        conn.commit()
    return stats
