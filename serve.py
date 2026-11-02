#!/usr/bin/env python3
"""PF Monitor — web application server (stdlib only).

    python3 serve.py              # http://127.0.0.1:8010/
    PORT=9000 python3 serve.py
    HOST=0.0.0.0 python3 serve.py # LAN

Implements the server side of the Power Factor Monitoring proposal:
ingest + validation (§7), PF/tariff engine (§8), review queue (§7.4),
self-observability alerts (§11.3), behind session authentication with
two roles: admin (application owner) and viewer (end user).

Access model:
  - every page/API requires a logged-in session, except /login,
    /static/* and the device-facing POST /api/ingest;
  - POST /api/review and /api/simulate require the admin role;
  - POST /api/ingest is for edge devices (no browser session) and
    remains loopback-only until device credentials are introduced.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from pfmon import alerts, auth, db, pipeline, simulator
from pfmon.configio import load_config
from pfmon.engine import ENERGY_REGS, REGISTERS, capacitor_kvar, summarize
from pfmon.simulator import IST

BASE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE, "ui")
DB_PATH = os.path.join(BASE, "data", "pf-monitor.db")

TARIFF, SETTINGS, METERS = load_config(BASE)
CONN = db.connect(DB_PATH)
auth.init(CONN)
WRITE_LOCK = threading.Lock()

SESSION_COOKIE = "pfsession"
MIME = {".html": "text/html", ".js": "application/javascript",
        ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png"}


def now_ist() -> datetime:
    return datetime.now(IST)


def series_for(meter_id: str) -> dict:
    return {r: db.accepted_series(CONN, meter_id, r) for r in ENERGY_REGS}


class Handler(BaseHTTPRequestHandler):
    server_version = "pf-monitor/0.2"

    # ------------------------------------------------------------- helpers
    def _json(self, obj, code: int = 200, headers: dict = None) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, rel: str) -> None:
        path = os.path.normpath(os.path.join(UI_DIR, rel))
        if not path.startswith(UI_DIR) or not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(os.path.splitext(path)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str) -> None:
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return ""

    def _user(self):
        return auth.session_user(CONN, self._cookie_token())

    @staticmethod
    def _set_cookie(token: str, max_age: int) -> dict:
        return {"Set-Cookie":
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; "
                f"SameSite=Lax; Max-Age={max_age}"}

    def log_message(self, fmt, *args):  # quiet access log
        pass

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = url.path

        # ---- public routes
        if route == "/login":
            return self._file("login.html")
        if route.startswith("/static/"):
            return self._file(route[len("/static/"):])

        # ---- everything else requires a session
        user = self._user()
        if user is None:
            if route == "/":
                return self._redirect("/login")
            return self._json({"error": "authentication required"}, 401)

        if route == "/":
            return self._file("index.html")

        if route == "/api/me":
            return self._json(user)

        if route == "/api/meters":
            out = []
            for mid, prof in METERS.items():
                out.append({"meter_id": mid, **prof, **db.meter_stats(CONN, mid)})
            return self._json(out)

        if route == "/api/summary":
            mid = q.get("meter_id")
            if mid not in METERS:
                return self._json({"error": "unknown meter_id"}, 400)
            s = summarize(series_for(mid), METERS[mid], TARIFF, now_ist())
            s["meter"] = {"meter_id": mid, **METERS[mid], **db.meter_stats(CONN, mid)}
            s["tariff_version"] = TARIFF.get("version")
            return self._json(s)

        if route == "/api/review":
            return self._json(pipeline.pending_reviews(CONN))

        if route == "/api/alerts":
            with WRITE_LOCK:
                alerts.evaluate(CONN, TARIFF, SETTINGS, METERS, now_ist(), series_for)
            return self._json(alerts.list_alerts(CONN))

        if route == "/api/config":
            return self._json({"tariff": TARIFF, "settings": SETTINGS,
                               "meters": METERS, "registers": list(REGISTERS)})

        if route == "/api/capacitor":
            try:
                kvar = capacitor_kvar(float(q["p_kw"]),
                                      float(q["pf_from"]), float(q["pf_to"]))
                return self._json({"kvar": kvar})
            except (KeyError, ValueError) as e:
                return self._json({"error": str(e)}, 400)

        self.send_error(404)

    # --------------------------------------------------------------- POST
    def do_POST(self):
        route = urlparse(self.path).path
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)

        # ---- login (public)
        if route == "/api/login":
            user = auth.verify(CONN, str(body.get("username", "")),
                               str(body.get("password", "")))
            if user is None:
                return self._json({"error": "invalid username or password"}, 401)
            token = auth.create_session(CONN, user["username"])
            db.audit(CONN, user["username"], "login")
            CONN.commit()
            return self._json(user, headers=self._set_cookie(
                token, auth.SESSION_HOURS * 3600))

        # ---- device-facing ingest: loopback-only, no browser session
        if route == "/api/ingest":
            if not self._loopback():
                return self._json({"error": "ingest is loopback-only "
                                   "(device credentials not yet configured)"}, 403)
            payloads = body if isinstance(body, list) else [body]
            results = []
            with WRITE_LOCK:
                for p in payloads:
                    results.append(pipeline.ingest(CONN, p, SETTINGS, METERS))
                CONN.commit()
            return self._json(results if isinstance(body, list) else results[0])

        # ---- everything else requires a session
        user = self._user()
        if user is None:
            return self._json({"error": "authentication required"}, 401)

        if route == "/api/logout":
            auth.destroy_session(CONN, self._cookie_token())
            db.audit(CONN, user["username"], "logout")
            CONN.commit()
            return self._json({"ok": True}, headers=self._set_cookie("", 0))

        # ---- admin-only actions
        if user["role"] != "admin":
            return self._json({"error": "admin role required"}, 403)

        if route == "/api/review":
            with WRITE_LOCK:
                res = pipeline.review(CONN, int(body.get("id", 0)),
                                      body.get("action", ""),
                                      user["username"],
                                      body.get("value"))
                CONN.commit()
            return self._json(res, 200 if res["result"] != "error" else 400)

        if route == "/api/simulate":
            days = int(body.get("days", 35))
            with WRITE_LOCK:
                stats = simulator.simulate(CONN, SETTINGS, METERS, days=days,
                                           seed=int(body.get("seed", 42)))
                n = alerts.evaluate(CONN, TARIFF, SETTINGS, METERS,
                                    now_ist(), series_for)
            db.audit(CONN, user["username"], "simulate", after=stats)
            CONN.commit()
            return self._json({"ok": True, "ingest": stats, "alerts_fired": n})

        self.send_error(404)


def main():
    created = auth.seed_defaults(CONN)
    if created:
        print("Seeded demo users (change before non-demo use):")
        for username, password, role in created:
            label = "super admin" if role == "admin" else "end user"
            print(f"  {username} / {password}  ({label})")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8010"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"PF Monitor serving on http://{host}:{port}/  (db: {DB_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
