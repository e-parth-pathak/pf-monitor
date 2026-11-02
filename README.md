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

Sign in, then press **Load demo data**: a simulator (stand-in for the
camera edge agents of proposal §6) pushes 35 days of 15-minute capture
rounds for three demo meters through the real ingest pipeline — one
healthy (incentive band), one heavily inductive (penalty band), one
over-compensated lead-dominant meter (high PF, no incentive per
Commercial Circular 312).

## Users & roles

Two accounts are seeded on first start (**demo credentials — change
before any non-demo exposure**; they are printed to the console and
shown on the login page):

| Account | Password | Role | Can |
|---|---|---|---|
| `admin` | `admin123` | super admin (owner) | everything: dashboards, adjudicate the review queue, run the simulator |
| `user` | `user123` | end user | read-only: dashboards, alerts, review queue (view), config |

Sessions are HttpOnly cookies backed by DB-stored tokens (12 h TTL);
passwords are salted PBKDF2-HMAC-SHA256. All role checks are enforced
server-side; the UI merely hides what the server would refuse.
`POST /api/ingest` is the device-facing endpoint — it uses no browser
session and stays loopback-only until device credentials are added.

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
| `POST /api/login` / `POST /api/logout` | session management |
| `GET /api/me` | current user + role |
| `GET /api/meters` | meter list + stats |
| `GET /api/summary?meter_id=` | cycle-to-date, projection, previous cycle, daily series |
| `POST /api/ingest` | reading(s) in the §9.1 schema (idempotent) |
| `GET /api/review` / `POST /api/review` | review queue / adjudicate (confirm, correct, reject) — **POST is admin-only** |
| `GET /api/alerts` | evaluate rules + list firings |
| `GET /api/config` | active tariff + meter profiles |
| `GET /api/capacitor?p_kw=&pf_from=&pf_to=` | Q = P(tanφ₁ − tanφ₂) |
| `POST /api/simulate` | `{days, seed}` — demo data — **admin-only** |

All routes except `/login`, `/static/*` and `POST /api/ingest` require a
session; `POST /api/ingest` refuses non-loopback callers.

**Demo caveat:** the energy rate in `tariff.yaml` is a marked placeholder;
real slabs/rates must come from the consumer's bill and the in-force MERC
tariff order (proposal Phase 0).
