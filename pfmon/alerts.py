"""Alert catalogue evaluation (proposal §11.3, business + data-gap rules).

Evaluation is idempotent: an alert fires at most once per rule/meter/day
(UNIQUE constraint + INSERT OR IGNORE)."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from . import db
from .engine import summarize


def evaluate(conn, tariff: dict, settings: dict, meters: dict,
             now: datetime, series_fetch) -> int:
    """series_fetch(meter_id) -> {register: [(ts, value), ...]}"""
    fired = 0
    rules = tariff["pf_rules"]
    for meter_id, profile in meters.items():
        series = series_fetch(meter_id)
        if not any(series.values()):
            continue
        s = summarize(series, profile, tariff, now)
        cyc, proj, daily = s.get("cycle"), s.get("projection"), s["daily"]

        # projected cycle PF in penalty band (critical, needs >=7 days basis)
        if (proj and cyc and proj["pf"] is not None
                and proj["pf"] < rules["penalty_below"]
                and cyc.get("days_elapsed", 0) >= 7):
            fired += _fire(conn, "projected_penalty", meter_id, "critical", now,
                           f"Projected cycle PF {proj['pf']:.3f} is in the penalty "
                           f"band (<{rules['penalty_below']}) — act before the bill")

        # cycle-to-date PF already in penalty band (warning, >=2 days)
        if (cyc and cyc.get("pf") is not None
                and cyc["pf"] < rules["penalty_below"]
                and cyc.get("days_elapsed", 0) >= 2):
            fired += _fire(conn, "cycle_penalty_to_date", meter_id, "warning", now,
                           f"Cycle-to-date PF {cyc['pf']:.3f} is below "
                           f"{rules['penalty_below']}")

        # daily PF below incentive threshold 3 consecutive complete days
        complete = [r for r in daily
                    if r.get("pf") is not None and r["day"] < now.date().isoformat()]
        if len(complete) >= 3 and all(
                r["pf"] < rules["incentive_above"] for r in complete[-3:]):
            fired += _fire(conn, "low_daily_pf", meter_id, "warning", now,
                           f"Daily PF below {rules['incentive_above']} for 3 "
                           f"consecutive days (latest {complete[-1]['pf']:.3f})")

        # lead-dominant drift on cycle-to-date deltas (forfeits incentive, §2.2)
        if cyc:
            d = cyc.get("deltas", {})
            lag, lead = d.get("rkvah_lag"), d.get("rkvah_lead")
            if lag is not None and lead is not None and lead > lag:
                fired += _fire(conn, "lead_dominant", meter_id, "warning", now,
                               f"RkVAh-lead ({lead:.0f}) exceeds RkVAh-lag "
                               f"({lag:.0f}) cycle-to-date — incentive forfeited "
                               f"(Comm. Circular 312)")

        # kVAh excess cost spike: yesterday > mean + 3σ of trailing 30 days
        costs = [(r["day"], r["excess_kvah"]) for r in daily
                 if r.get("excess_kvah") is not None
                 and r["day"] < now.date().isoformat()]
        if len(costs) >= 10:
            *hist, (yday, yval) = costs[-31:]
            vals = [v for _, v in hist]
            mean = statistics.fmean(vals)
            sd = statistics.pstdev(vals)
            if sd > 0 and yval > mean + 3 * sd:
                fired += _fire(conn, "excess_spike", meter_id, "warning", now,
                               f"kVAh excess on {yday} ({yval:.0f} kVAh) is > "
                               f"mean+3σ of the trailing month ({mean:.0f}±{sd:.0f})")

        # data gap: no accepted reading for > 4 capture intervals (critical)
        last_ts = max((s0[-1][0] for s0 in series.values() if s0), default=None)
        if last_ts:
            gap_limit = timedelta(minutes=4 * settings.get("interval_minutes", 15))
            try:
                last_dt = datetime.fromisoformat(last_ts)
                if now - last_dt > gap_limit:
                    fired += _fire(conn, "data_gap", meter_id, "critical", now,
                                   f"No accepted reading since {last_ts} "
                                   f"(> 4 capture intervals)")
            except ValueError:
                pass
    conn.commit()
    return fired


def _fire(conn, rule: str, meter_id: str, severity: str,
          now: datetime, message: str) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO alerts (rule, meter_id, severity, fired_on,"
        " fired_at, message) VALUES (?,?,?,?,?,?)",
        (rule, meter_id, severity, now.date().isoformat(),
         now.isoformat(timespec="seconds"), message),
    )
    if cur.rowcount:
        db.audit(conn, "alerter", "alert_fired", f"alert:{rule}:{meter_id}",
                 after={"severity": severity, "message": message})
    return cur.rowcount


def list_alerts(conn, limit: int = 100):
    rows = conn.execute(
        "SELECT * FROM alerts ORDER BY fired_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
