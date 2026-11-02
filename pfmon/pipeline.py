"""Server ingest pipeline (proposal §7): idempotent, validating, auditable.

A reading is accepted, queued for review, or rejected with an audit entry —
nothing is silently dropped. Checks, in order:
  1. schema / known meter
  2. duplicate (device_id, register, ts)  -> no-op (store-and-forward replays)
  3. OCR confidence below τ               -> needs_review
  4. monotonicity of cumulative registers -> needs_review (md_kva exempt:
     it is a cycle-maximum register that resets at cycle start)
  5. implied-power rate limit (kwh)       -> needs_review
  6. cross-register consistency per capture round:
     kVAh² ≈ kWh² + (RkVAh_lag + RkVAh_lead)²  -> flags the whole round
"""
from __future__ import annotations

import math

from . import db
from .engine import REGISTERS, unwrap_delta

CROSS_CHECK_REGS = ("kwh", "rkvah_lag", "rkvah_lead", "kvah")


def ingest(conn, payload: dict, settings: dict, meters: dict,
           actor: str = "edge") -> dict:
    # --- 1. schema
    missing = [k for k in ("device_id", "meter_id", "register", "ts", "value")
               if payload.get(k) in (None, "")]
    if missing:
        return {"result": "rejected", "reason": f"missing fields: {missing}"}
    register = payload["register"]
    if register not in REGISTERS:
        return {"result": "rejected", "reason": f"unknown register {register!r}"}
    meter_id = payload["meter_id"]
    profile = meters.get(meter_id)
    if profile is None:
        return {"result": "rejected", "reason": f"unknown meter {meter_id!r}"}
    try:
        value = float(payload["value"])
    except (TypeError, ValueError):
        return {"result": "rejected", "reason": "value is not a number"}
    device_id, ts = payload["device_id"], payload["ts"]
    confidence = payload.get("confidence")

    # --- 2. idempotency (§7.1)
    dup = conn.execute(
        "SELECT id FROM readings WHERE device_id=? AND register=? AND ts=? LIMIT 1",
        (device_id, register, ts),
    ).fetchone()
    if dup:
        return {"result": "duplicate", "id": dup["id"]}

    status, reason = "auto", None

    # --- 3. confidence gate (edge τ, §6.3)
    tau = settings.get("confidence_threshold", 0.90)
    if confidence is not None and confidence < tau:
        status, reason = "needs_review", f"low_confidence ({confidence:.2f} < {tau})"

    # --- 4./5. monotonicity + rate limit against last accepted value (§7.2)
    prev = db.last_accepted_before(conn, meter_id, register, ts)
    fullscale = profile.get("register_fullscale")
    if status == "auto" and prev and register != "md_kva":
        prev_ts, prev_val = prev
        delta = unwrap_delta(prev_val, value, fullscale)
        if delta < 0:
            status, reason = "needs_review", (
                f"monotonicity: {value} < previous accepted {prev_val}")
        elif register == "kwh":
            hours = _hours_between(prev_ts, ts)
            if hours > 0:
                implied_kw = delta / hours
                limit = profile.get("sanctioned_kw", 0) * settings.get("rate_headroom", 1.5)
                if limit and implied_kw > limit:
                    status, reason = "needs_review", (
                        f"rate_limit: implied {implied_kw:.0f} kW > {limit:.0f} kW")

    cur = conn.execute(
        "INSERT INTO readings (device_id, meter_id, register, ts, value, unit,"
        " confidence, status, flag_reason, resolution, capture_round, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (device_id, meter_id, register, ts, value, payload.get("unit"),
         confidence, status, reason,
         "pending" if status == "needs_review" else None,
         payload.get("capture_round"), db.utcnow()),
    )
    rid = cur.lastrowid
    if status == "needs_review":
        db.audit(conn, actor, "reading_flagged", f"reading:{rid}",
                 after={"reason": reason, "value": value, "register": register})

    # --- 6. cross-register consistency once the round is complete (§7.2)
    round_id = payload.get("capture_round")
    if round_id and status == "auto":
        _cross_register_check(conn, meter_id, round_id, fullscale, actor)

    return {"result": "flagged" if status == "needs_review" else "accepted",
            "id": rid, "reason": reason}


def _hours_between(ts_a: str, ts_b: str) -> float:
    from datetime import datetime

    try:
        a = datetime.fromisoformat(ts_a)
        b = datetime.fromisoformat(ts_b)
        return (b - a).total_seconds() / 3600.0
    except ValueError:
        return 0.0


def _cross_register_check(conn, meter_id: str, round_id: str,
                          fullscale, actor: str) -> None:
    rows = conn.execute(
        "SELECT * FROM readings WHERE capture_round=? AND meter_id=? "
        "AND status='auto' AND superseded_by IS NULL",
        (round_id, meter_id),
    ).fetchall()
    by_reg = {r["register"]: r for r in rows}
    if not all(r in by_reg for r in CROSS_CHECK_REGS):
        return  # round incomplete (or parts already flagged) — nothing to do
    deltas = {}
    prev_ts = set()
    for reg in CROSS_CHECK_REGS:
        row = by_reg[reg]
        prev = db.last_accepted_before(conn, meter_id, reg, row["ts"])
        if prev is None:
            return  # first round for this meter: no baseline yet
        prev_ts.add(prev[0])
        deltas[reg] = unwrap_delta(prev[1], row["value"], fullscale)
    if len(prev_ts) != 1:
        # Baselines come from different capture rounds (an earlier reading of
        # one register was flagged), so the deltas span different windows and
        # the kVAh identity cannot be checked — skip rather than false-flag.
        return
    expected = math.sqrt(
        deltas["kwh"] ** 2 + (deltas["rkvah_lag"] + deltas["rkvah_lead"]) ** 2)
    err = abs(deltas["kvah"] - expected)
    tol = max(0.20 * expected, 1.0)  # generous: OCR last-digit noise is normal
    if err <= tol:
        return
    ids = [by_reg[r]["id"] for r in CROSS_CHECK_REGS]
    reason = (f"cross_register: ΔkVAh={deltas['kvah']:.1f} vs "
              f"√(ΔkWh²+(ΣΔRkVAh)²)={expected:.1f}")
    conn.executemany(
        "UPDATE readings SET status='needs_review', flag_reason=?, resolution='pending' "
        "WHERE id=? AND status='auto'",
        [(reason, i) for i in ids],
    )
    db.audit(conn, actor, "round_flagged", f"round:{round_id}",
             after={"reason": reason, "reading_ids": ids})


# ----------------------------------------------------------------- review

def review(conn, reading_id: int, action: str, reviewer: str,
           corrected_value=None) -> dict:
    """Adjudicate a flagged reading (§7.4). confirm/correct insert a new
    human_verified row superseding the flagged one; reject leaves the
    reading out of all computation. The original row is never mutated
    beyond its supersede/resolution bookkeeping (§9.3)."""
    row = conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
    if row is None:
        return {"result": "error", "reason": "no such reading"}
    if row["status"] != "needs_review" or row["resolution"] not in (None, "pending"):
        return {"result": "error", "reason": "reading is not pending review"}

    now = db.utcnow()
    if action == "reject":
        conn.execute(
            "UPDATE readings SET resolution='rejected', reviewed_by=?, reviewed_at=? "
            "WHERE id=?", (reviewer, now, reading_id))
        db.audit(conn, reviewer, "review_rejected", f"reading:{reading_id}",
                 before={"value": row["value"]})
        return {"result": "rejected"}

    if action == "confirm":
        new_value, resolution = row["value"], "confirmed"
    elif action == "correct":
        if corrected_value is None:
            return {"result": "error", "reason": "corrected value required"}
        new_value, resolution = float(corrected_value), "corrected"
    else:
        return {"result": "error", "reason": f"unknown action {action!r}"}

    cur = conn.execute(
        "INSERT INTO readings (device_id, meter_id, register, ts, value, unit,"
        " confidence, status, capture_round, supersedes, reviewed_by, reviewed_at,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["device_id"], row["meter_id"], row["register"], row["ts"], new_value,
         row["unit"], row["confidence"], "human_verified", row["capture_round"],
         reading_id, reviewer, now, now),
    )
    new_id = cur.lastrowid
    conn.execute(
        "UPDATE readings SET superseded_by=?, resolution=?, reviewed_by=?, reviewed_at=? "
        "WHERE id=?", (new_id, resolution, reviewer, now, reading_id))
    db.audit(conn, reviewer, f"review_{resolution}", f"reading:{reading_id}",
             before={"value": row["value"]},
             after={"value": new_value, "new_id": new_id})
    return {"result": resolution, "new_id": new_id}


def pending_reviews(conn, limit: int = 200):
    rows = conn.execute(
        "SELECT id, device_id, meter_id, register, ts, value, confidence,"
        " flag_reason, capture_round FROM readings "
        "WHERE status='needs_review' AND (resolution IS NULL OR resolution='pending') "
        "ORDER BY ts DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
