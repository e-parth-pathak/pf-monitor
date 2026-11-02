#!/usr/bin/env python3
"""PF Monitor — web application server (stdlib only).

    python3 serve.py              # http://127.0.0.1:8010/
    PORT=9000 python3 serve.py
    HOST=0.0.0.0 python3 serve.py # LAN (write endpoints stay loopback-only)

Implements the server side of the Power Factor Monitoring proposal:
ingest + validation (§7), PF/tariff engine (§8), review queue (§7.4),
self-observability alerts (§11.3). The meter simulator (§6 stand-in)
is triggered from the UI or POST /api/simulate.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from pfmon import alerts, db, pipeline, simulator
from pfmon.configio import load_config
from pfmon.engine import ENERGY_REGS, REGISTERS, capacitor_kvar, summarize
from pfmon.simulator import IST

BASE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE, "ui")
DB_PATH = os.path.join(BASE, "data", "pf-monitor.db")

TARIFF, SETTINGS, METERS = load_config(BASE)
CONN = db.connect(DB_PATH)
WRITE_LOCK = threading.Lock()

MIME = {".html": "text/html", ".js": "application/javascript",
        ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png"}


def now_ist() -> datetime:
    return datetime.now(IST)


def series_for(meter_id: str) -> dict:
    return {r: db.accepted_series(CONN, meter_id, r) for r in ENERGY_REGS}


class Handler(BaseHTTPRequestHandler):
    server_version = "pf-monitor/0.1"

    # ------------------------------------------------------------- helpers
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def log_message(self, fmt, *args):  # quiet access log
        pass

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = url.path

        if route == "/":
            return self._file("index.html")
        if route.startswith("/static/"):
            return self._file(route[len("/static/"):])

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
        if not self._loopback():
            return self._json({"error": "write endpoints are loopback-only"}, 403)
        route = urlparse(self.path).path
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)

        if route == "/api/ingest":
            payloads = body if isinstance(body, list) else [body]
            results = []
            with WRITE_LOCK:
                for p in payloads:
                    results.append(pipeline.ingest(CONN, p, SETTINGS, METERS))
                CONN.commit()
            return self._json(results if isinstance(body, list) else results[0])

        if route == "/api/review":
            with WRITE_LOCK:
                res = pipeline.review(CONN, int(body.get("id", 0)),
                                      body.get("action", ""),
                                      body.get("reviewer", "web-ui"),
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
            return self._json({"ok": True, "ingest": stats, "alerts_fired": n})

        self.send_error(404)


def main():
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
