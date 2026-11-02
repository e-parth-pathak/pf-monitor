# PF Monitor

Web implementation of the Power Factor Monitoring System proposal
(`../pf-monitor-proposal/`). Pure Python 3 standard library + SQLite +
a browser — no build step, no dependencies.

## Run

```bash
python3 serve.py          # http://127.0.0.1:8010/
PORT=9000 python3 serve.py
HOST=0.0.0.0 python3 serve.py   # LAN; write endpoints stay loopback-only
```

Open the UI and press **Load demo data**: a simulator (stand-in for the
camera edge agents of proposal §6) pushes 35 days of 15-minute capture
rounds for three demo meters through the real ingest pipeline — one
healthy (incentive band), one heavily inductive (penalty band), one
over-compensated lead-dominant meter (high PF, no incentive per
Commercial Circular 312).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_engine.py` pins the worked examples A/B/C from proposal §2.5
and the 145 kVAr capacitor-sizing example from §8.5.

## What is implemented (proposal section map)

| Proposal | Here |
|---|---|
| §2 regulatory rules | `config/tariff.yaml` (versioned config, no constants in code), `pfmon/engine.py` |
| §6 edge pipeline | simulated by `pfmon/simulator.py` (real hardware replaces it; `POST /api/ingest` is the device-facing endpoint) |
| §7 server pipeline | `pfmon/pipeline.py`: idempotency, confidence gate, monotonicity, rate limit, cross-register consistency, review adjudication |
| §8 computation engine | `pfmon/engine.py`: window PF, rollover unwrap, cycle summary, kVAh excess cost, capacitor sizing, month-end projection |
| §9 data model | `pfmon/db.py`: immutable readings + supersede links, append-only `audit_log` |
| §10 storage adapter | SQLite here; the engine is pure functions over series, so a Timescale/Elastic adapter swaps in behind `db.accepted_series` |
| §11 observability | `pfmon/alerts.py`: projected-penalty, cycle-penalty, low daily PF, lead-dominant, excess spike, data gap |

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/meters` | meter list + stats |
| `GET /api/summary?meter_id=` | cycle-to-date, projection, previous cycle, daily series |
| `POST /api/ingest` | reading(s) in the §9.1 schema (idempotent) |
| `GET /api/review` / `POST /api/review` | review queue / adjudicate (confirm, correct, reject) |
| `GET /api/alerts` | evaluate rules + list firings |
| `GET /api/config` | active tariff + meter profiles |
| `GET /api/capacitor?p_kw=&pf_from=&pf_to=` | Q = P(tanφ₁ − tanφ₂) |
| `POST /api/simulate` | `{days, seed}` — demo data |

POST endpoints refuse non-loopback callers.

**Demo caveat:** the energy rate in `tariff.yaml` is a marked placeholder;
real slabs/rates must come from the consumer's bill and the in-force MERC
tariff order (proposal Phase 0).
