"""PF & tariff computation engine (proposal §8).

All functions here are pure: they take series/config and return values,
so the worked examples from proposal §2.5 pin them in tests. Database
access lives in serve.py / pipeline.py.

Regulatory basis (see proposal §2 and Appendix B):
  PF = kWh / sqrt(kWh^2 + (RkVAh_lag + RkVAh_lead)^2)   [MSEDCL, Dec 2018]
  nature = lead if RkVAh_lead > RkVAh_lag else lag       [Comm. Circular 312]
  no incentive when lead-dominant; penalty < 0.90 either way
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

REGISTERS = ("kwh", "rkvah_lag", "rkvah_lead", "kvah", "md_kva")
ENERGY_REGS = ("kwh", "rkvah_lag", "rkvah_lead", "kvah")


# ---------------------------------------------------------------- core math

def compute_pf(kwh: float, rkvah_lag: float, rkvah_lead: float):
    """Monthly/window PF per the MSEDCL 2018 methodology (§2.1)."""
    denom = math.sqrt(kwh ** 2 + (rkvah_lag + rkvah_lead) ** 2)
    pf = kwh / denom if denom else None
    nature = "lead" if rkvah_lead > rkvah_lag else "lag"
    return pf, nature


def classify(pf, nature: str, rules: dict) -> str:
    """PENALTY / NEUTRAL / INCENTIVE per §2.3, incl. the Circular 312 test."""
    if pf is None:
        return "NO_DATA"
    if pf < rules["penalty_below"]:
        return "PENALTY"
    if pf > rules["incentive_above"]:
        if nature == "lead" and rules.get("no_incentive_if_lead_dominant", True):
            return "NEUTRAL"  # lead-dominant forfeits the incentive
        return "INCENTIVE"
    return "NEUTRAL"


def unwrap_delta(prev: float, curr: float, fullscale) -> float:
    """Delta of a cumulative register, unwrapping display rollover (§8.1)."""
    d = curr - prev
    if d < 0 and fullscale and prev > 0.8 * fullscale:
        d += fullscale
    return d


def capacitor_kvar(p_kw: float, pf_from: float, pf_to: float) -> float:
    """Q = P (tan phi1 - tan phi2) — sizing guidance only (§8.5)."""
    for pf in (pf_from, pf_to):
        if not 0 < pf <= 1:
            raise ValueError("power factor must be in (0, 1]")
    if pf_to <= pf_from:
        raise ValueError("target PF must exceed current PF")
    return p_kw * (math.tan(math.acos(pf_from)) - math.tan(math.acos(pf_to)))


# ------------------------------------------------------------ series helpers
# A "series" is a list of (ts_iso, value) for one register, sorted by ts,
# containing accepted readings only (status auto/human_verified, §7).

def last_per_day(series) -> dict:
    out: dict = {}
    for ts, v in series:
        out[ts[:10]] = v  # sorted input: later ts wins
    return out


def daily_deltas(series, fullscale) -> dict:
    """day -> delta (last reading of day minus last reading of prev data-day)."""
    lp = last_per_day(series)
    days = sorted(lp)
    return {
        d: unwrap_delta(lp[prev], lp[d], fullscale)
        for prev, d in zip(days, days[1:])
    }


def window_delta(series, start_iso: str, end_iso: str, fullscale):
    """Register delta over [start, end): last value before end minus baseline.

    Baseline is the last reading before start, or the first reading inside
    the window when none precedes it (meter commissioned mid-window)."""
    baseline = None
    last = None
    first_inside = None
    for ts, v in series:
        if ts < start_iso:
            baseline = v
        elif ts < end_iso:
            if first_inside is None:
                first_inside = v
            last = v
        else:
            break
    if last is None:
        return None
    base = baseline if baseline is not None else first_inside
    return unwrap_delta(base, last, fullscale)


# ------------------------------------------------------------ billing cycles

def cycle_start(d: date, anchor_day: int) -> date:
    if d.day >= anchor_day:
        return d.replace(day=anchor_day)
    prev_month_last = d.replace(day=1) - timedelta(days=1)
    return prev_month_last.replace(day=anchor_day)


def next_cycle_start(d: date, anchor_day: int) -> date:
    cs = cycle_start(d, anchor_day)
    nxt = (cs.replace(day=1) + timedelta(days=32)).replace(day=1)
    return nxt.replace(day=anchor_day)


# ------------------------------------------------------------------ summary

def _window_view(series_by_reg, start_iso, end_iso, fullscale, rules, rate):
    deltas = {
        r: window_delta(series_by_reg.get(r, []), start_iso, end_iso, fullscale)
        for r in ENERGY_REGS
    }
    core = [deltas["kwh"], deltas["rkvah_lag"], deltas["rkvah_lead"]]
    if any(v is None for v in core):
        return None
    pf, nature = compute_pf(*core)
    band = classify(pf, nature, rules)
    excess = None
    cost = None
    if deltas["kvah"] is not None:
        excess = max(deltas["kvah"] - deltas["kwh"], 0.0)
        cost = excess * rate if rate else None
    return {
        "deltas": deltas,
        "pf": pf,
        "nature": nature,
        "band": band,
        "excess_kvah": excess,
        "excess_cost_inr": cost,
    }


def summarize(series_by_reg: dict, profile: dict, tariff: dict, now: datetime) -> dict:
    """Full meter summary: current cycle to date, projection, previous cycle,
    and daily series for charts. Pure function of its inputs (§8)."""
    rules = tariff["pf_rules"]
    rate = tariff.get("energy_rate_inr_per_kvah")
    fullscale = profile.get("register_fullscale")
    anchor = int(profile.get("cycle_anchor_day", 1))

    today = now.date()
    cstart = cycle_start(today, anchor)
    cend = next_cycle_start(today, anchor)
    pstart = cycle_start(cstart - timedelta(days=1), anchor)

    iso = lambda d: d.isoformat()  # noqa: E731
    now_iso = now.isoformat()

    current = _window_view(series_by_reg, iso(cstart), now_iso, fullscale, rules, rate)
    previous = _window_view(series_by_reg, iso(pstart), iso(cstart), fullscale, rules, rate)

    # ---- daily series (for charts + projection)
    dd = {r: daily_deltas(series_by_reg.get(r, []), fullscale) for r in ENERGY_REGS}
    all_days = sorted(set().union(*[set(v) for v in dd.values()]) if dd else set())
    daily = []
    for d in all_days:
        row = {"day": d}
        k, lg, ld = dd["kwh"].get(d), dd["rkvah_lag"].get(d), dd["rkvah_lead"].get(d)
        if None not in (k, lg, ld):
            pf, nature = compute_pf(k, lg, ld)
            row.update(pf=pf, nature=nature, band=classify(pf, nature, rules), dkwh=k)
        kv = dd["kvah"].get(d)
        if kv is not None and k is not None:
            ex = max(kv - k, 0.0)
            row["excess_kvah"] = ex
            if rate:
                row["excess_cost_inr"] = ex * rate
        daily.append(row)

    # ---- month-end projection (§8.6): cycle-to-date + trailing-7-day average
    projection = None
    if current:
        days_elapsed = max((today - cstart).days, 1)
        days_total = (cend - cstart).days
        remaining = max(days_total - days_elapsed, 0)
        complete_days = [d for d in all_days if d < today.isoformat()]
        tail = complete_days[-7:]
        proj_deltas = {}
        for r in ("kwh", "rkvah_lag", "rkvah_lead", "kvah"):
            vals = [dd[r][d] for d in tail if d in dd[r]]
            avg = sum(vals) / len(vals) if vals else 0.0
            base = current["deltas"].get(r)
            proj_deltas[r] = (base or 0.0) + avg * remaining
        ppf, pnature = compute_pf(
            proj_deltas["kwh"], proj_deltas["rkvah_lag"], proj_deltas["rkvah_lead"]
        )
        projection = {
            "pf": ppf,
            "nature": pnature,
            "band": classify(ppf, pnature, rules),
            "basis_days": len(tail),
            "remaining_days": remaining,
        }
        if rate:
            projection["excess_cost_inr"] = max(
                proj_deltas["kvah"] - proj_deltas["kwh"], 0.0
            ) * rate
        current["days_elapsed"] = days_elapsed
        current["days_total"] = days_total
        # average load over the cycle so far, for the capacitor widget (§8.5)
        hours = max((now - datetime.combine(cstart, datetime.min.time(),
                                            tzinfo=now.tzinfo)).total_seconds() / 3600, 1)
        current["avg_kw"] = (current["deltas"]["kwh"] or 0.0) / hours

    return {
        "cycle": {"start": iso(cstart), "end": iso(cend), **(current or {})} if current else None,
        "previous_cycle": {"start": iso(pstart), "end": iso(cstart), **(previous or {})} if previous else None,
        "projection": projection,
        "daily": daily,
    }
