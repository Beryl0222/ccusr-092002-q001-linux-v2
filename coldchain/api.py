"""HTTP 接口：把治理规则暴露为 JSON API。

路由保持轻量，业务规则全部在 coldchain 包内；存储可选 JSONL 持久化。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import (
    AppendOnlyStore,
    Binding,
    CalibrationCertificate,
    ClockDrift,
    Device,
    Lot,
    PermissionDenied,
    amend_report,
    assess_lot,
    bind_device,
    carrier_view,
    claims_evidence_package,
    confirm_report,
    coverage_report,
    ingest_reading,
    maintenance_plan,
    publish_curve,
    recompute_report,
    register_calibration,
    register_device,
    register_drift,
    register_lot,
    report_status,
    save_report,
)
from .corrections import latest_curve
from .reports import get_report


def make_handler(store: AppendOnlyStore, health_payload):
    """构造绑定指定存储与身份信息的请求处理器。"""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if method == "GET" and path == "/health":
                    return self._send(200, health_payload())
                if method == "POST":
                    return self._post(path)
                if method == "GET":
                    return self._get(path, query)
                self._send(405, {"error": "method not allowed"})
            except PermissionDenied as exc:
                self._send(403, {"error": str(exc)})
            except (KeyError, ValueError) as exc:
                self._send(400, {"error": str(exc)})

        def _post(self, path: str):
            body = self._body()
            if path == "/lots":
                return self._send(201, register_lot(store, Lot(**body)))
            if path == "/devices":
                return self._send(201, register_device(store, Device(**body)))
            if path == "/calibrations":
                return self._send(201, register_calibration(store, CalibrationCertificate(**body)))
            if path == "/drifts":
                return self._send(201, register_drift(store, ClockDrift(**body)))
            if path == "/bindings":
                return self._send(201, bind_device(store, Binding(**body)))
            if path == "/readings":
                return self._send(201, ingest_reading(store, **body))
            if path == "/curves":
                return self._send(201, publish_curve(store, **body))
            if path == "/assessments":
                report = assess_lot(
                    store,
                    body["lot_id"],
                    body["segment_id"],
                    body["window_start"],
                    body["window_end"],
                )
                return self._send(201, save_report(store, report))
            if path.startswith("/reports/") and path.endswith("/confirm"):
                report_id = path.split("/")[2]
                return self._send(201, confirm_report(store, report_id, body["party"]))
            if path.startswith("/reports/") and path.endswith("/amend"):
                report_id = path.split("/")[2]
                return self._send(201, amend_report(store, report_id, reason=body["reason"]))
            self._send(404, {"error": "not found"})

        def _get(self, path: str, query: dict):
            if path == "/analytics/coverage":
                return self._send(200, {"rows": coverage_report(store)})
            if path == "/maintenance/plan":
                return self._send(200, {"tasks": maintenance_plan(store, as_of=query.get("as_of", [None])[0])})
            if path.startswith("/curves/"):
                curve = latest_curve(store, path.split("/")[2])
                return self._send(200 if curve else 404, curve or {"error": "not found"})
            if path.startswith("/reports/") and path.endswith("/evidence"):
                report_id = path.split("/")[2]
                return self._send(200, claims_evidence_package(store, report_id))
            if path.startswith("/reports/") and path.endswith("/status"):
                return self._send(200, report_status(store, path.split("/")[2]))
            if path.startswith("/reports/"):
                return self._send(200, get_report(store, path.split("/")[2]))
            if path.startswith("/disputes/") and path.endswith("/recompute"):
                return self._send(200, recompute_report(store, path.split("/")[2]))
            if path.startswith("/lots/") and path.endswith("/carrier-view"):
                carrier_id = query.get("carrier_id", [""])[0]
                return self._send(200, carrier_view(store, carrier_id, path.split("/")[2]))
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_args):
            return

    return Handler
