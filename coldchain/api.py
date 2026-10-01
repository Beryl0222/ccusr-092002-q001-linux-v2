"""HTTP 接口与基于令牌的角色边界。

角色：
- admin   行业协会：登记批次 / 设备 / 区段，发起判定、签署报告、查看全量数据。
- lab     校准实验室（实验人员）：登记证书、时钟核查，发布版本化修正曲线。
- carrier 承运商：只能查看与写入自己负责区段 / 自有设备的数据。
- claims  理赔人员：只能获得脱敏后的证据包。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import logic
from .store import Store

CST = timezone(timedelta(hours=8))

DEV_TOKENS = [
    ("dev-admin-token", "admin", "ASSOCIATION", "协会开发令牌"),
    ("dev-lab-token", "lab", "LAB-YN-01", "云南热作所校准实验室开发令牌"),
    ("dev-carrier-token", "carrier", "CARRIER-YX-01", "承运商一公司开发令牌"),
    ("dev-claims-token", "claims", "INSURER-01", "保险查勘开发令牌"),
]

ROLE_LABELS = {
    "admin": "行业协会",
    "lab": "校准实验室",
    "carrier": "承运商",
    "claims": "理赔",
}


def server_now():
    return datetime.now(CST).replace(microsecond=0).isoformat()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def require_fields(body, fields):
    missing = [f for f in fields if body.get(f) is None]
    if missing:
        raise ApiError(400, f"缺少必填字段: {', '.join(missing)}")


class App:
    """路由与鉴权集中处；处理函数只接收已鉴权身份。"""

    def __init__(self, store=None, seed_dev_tokens=False, now_factory=server_now):
        self.store = store or Store()
        self.now = now_factory
        if seed_dev_tokens:
            self.seed_dev_tokens()

    def seed_dev_tokens(self):
        for token, role, party, label in DEV_TOKENS:
            if self.store.get_token(token) is None:
                self.store.add_token(token, role, party, label, self.now())

    # -- 鉴权 -------------------------------------------------------------

    def _identity(self, headers):
        auth = headers.get("Authorization") or headers.get("authorization")
        if not auth or not auth.startswith("Bearer "):
            raise ApiError(401, "缺少 Bearer 令牌")
        token = auth[len("Bearer "):].strip()
        row = self.store.get_token(token)
        if row is None:
            raise ApiError(401, "令牌无效或已吊销")
        return row

    def _require(self, headers, roles):
        ident = self._identity(headers)
        if ident["role"] not in roles:
            raise ApiError(403, f"该操作需要角色: {'/'.join(roles)}，"
                                f"当前为 {ROLE_LABELS.get(ident['role'])}")
        return ident

    # -- 路由 -------------------------------------------------------------

    def dispatch(self, method, path, headers, raw):
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(400, "请求体必须是 UTF-8 JSON")

        p = path.rstrip("/") or "/"
        m = re.fullmatch(r"/api/instruments/([^/]+)/chain", p)
        if method == "GET" and m:
            return self.chain(self._require(headers, {"admin", "lab"}), m.group(1))
        m = re.fullmatch(r"/api/lots/([^/]+)/readings", p)
        if method == "GET" and m:
            return self.list_readings(self._identity(headers), m.group(1))
        m = re.fullmatch(r"/api/lots/([^/]+)/assessments/latest", p)
        if method == "GET" and m:
            return self.latest_assessment(self._identity(headers), m.group(1))
        m = re.fullmatch(r"/api/lots/([^/]+)/assessments", p)
        if method == "POST" and m:
            return self.run_assessment(self._require(headers, {"admin"}),
                                       m.group(1), body)
        m = re.fullmatch(r"/api/lots/([^/]+)/reports", p)
        if method == "POST" and m:
            return self.create_report(self._require(headers, {"admin"}),
                                      m.group(1), body)
        m = re.fullmatch(r"/api/lots/([^/]+)/reports/([^/]+)/evidence-package", p)
        if method == "POST" and m:
            return self.make_package(
                self._require(headers, {"claims", "admin"}),
                m.group(1), m.group(2), body)
        m = re.fullmatch(r"/api/reports/([^/]+)/sign", p)
        if method == "POST" and m:
            return self.sign_report(self._require(headers, {"admin"}),
                                    m.group(1), body)
        m = re.fullmatch(r"/api/reports/([^/]+)/confirmations", p)
        if method == "POST" and m:
            return self.confirm(self._identity(headers), m.group(1), body)
        m = re.fullmatch(r"/api/lots/([^/]+)/reports/([^/]+)/recompute", p)
        if method == "GET" and m:
            return self.recompute(self._require(headers, {"admin"}),
                                  m.group(1), m.group(2))
        m = re.fullmatch(r"/api/reports/([^/]+)", p)
        if method == "GET" and m:
            return self.get_report(self._identity(headers), m.group(1))
        m = re.fullmatch(r"/api/packages/([^/]+)", p)
        if method == "GET" and m:
            return self.get_package(
                self._require(headers, {"claims", "admin"}), m.group(1))
        m = re.fullmatch(r"/api/segments/([^/]+)", p)
        if method == "GET" and m:
            return self.get_segment(self._identity(headers), m.group(1))

        routes = {
            ("POST", "/api/instruments"): (
                {"admin", "carrier"}, self.add_instrument),
            ("POST", "/api/lots"): ({"admin"}, self.add_lot),
            ("POST", "/api/calibrations"): ({"lab", "admin"}, self.add_calibration),
            ("POST", "/api/clock-drifts"): ({"lab", "admin"}, self.add_clock_drift),
            ("POST", "/api/correction-curves"): (
                {"lab"}, self.add_correction_curve),
            ("POST", "/api/segments"): ({"admin"}, self.add_segment),
            ("POST", "/api/installations"): ({"admin"}, self.add_installation),
            ("POST", "/api/readings"): (
                {"admin", "carrier"}, self.append_reading),
            ("GET", "/api/my/segments"): ({"carrier"}, self.my_segments),
            ("GET", "/api/coverage"): ({"admin"}, self.coverage),
            ("POST", "/api/plans/generate"): ({"admin"}, self.generate_plans),
            ("GET", "/api/plans"): ({"admin", "lab"}, self.list_plans),
        }
        entry = routes.get((method, p))
        if entry is None:
            raise ApiError(404, f"未知路径: {method} {p}")
        ident = self._require(headers, entry[0])
        return entry[1](ident, body)

    # -- 治理登记 ----------------------------------------------------------

    def add_instrument(self, ident, body):
        require_fields(body, ["model", "kind"])
        carrier_id = body.get("carrier_id")
        if ident["role"] == "carrier":
            carrier_id = ident["party_id"]
        instrument_id = self.store.add_instrument(
            body["model"], body["kind"], carrier_id, self.now(),
            body.get("instrument_id"))
        return 201, {"instrument_id": instrument_id}

    def add_lot(self, ident, body):
        require_fields(body, ["lot_id"])
        self.store.add_lot(
            body["lot_id"], body.get("cultivar"), body.get("route_id"),
            body.get("packaging"), body.get("season"),
            body.get("harvested_at"), body.get("stems"), self.now())
        return 201, {"lot_id": body["lot_id"]}

    def add_calibration(self, ident, body):
        require_fields(body, ["instrument_id", "lab", "valid_from",
                              "valid_until", "max_abs_error_c"])
        cert_id = self.store.add_calibration(
            body["instrument_id"], body["lab"], body["valid_from"],
            body["valid_until"], float(body["max_abs_error_c"]), self.now())
        return 201, {"cert_id": cert_id}

    def add_clock_drift(self, ident, body):
        require_fields(body, ["instrument_id", "checked_at",
                              "device_clock_at"])
        drift_id = self.store.add_clock_drift(
            body["instrument_id"], body["checked_at"], body["device_clock_at"],
            float(body.get("ppm", 0.0)), self.now())
        return 201, {"drift_id": drift_id}

    def add_correction_curve(self, ident, body):
        require_fields(body, ["kind", "params", "valid_from"])
        if body.get("instrument_id") is None and body.get("instrument_model") is None:
            raise ApiError(400, "修正曲线必须绑定 instrument_id 或 instrument_model")
        curve_id, version = self.store.add_curve(
            body.get("instrument_id"), body.get("instrument_model"),
            body["kind"], json.dumps(body["params"], ensure_ascii=False),
            body["valid_from"], ident["party_id"], self.now(), body.get("note"))
        return 201, {"curve_id": curve_id, "version": version,
                     "published_by": ident["party_id"]}

    def add_segment(self, ident, body):
        require_fields(body, ["lot_id", "carrier_id", "name",
                              "start_at", "end_at"])
        segment_id = self.store.add_segment(
            body["lot_id"], body["carrier_id"], body["name"],
            body["start_at"], body["end_at"], body.get("limit_min_c"),
            body.get("limit_max_c"), int(body.get("nominal_interval_s", 300)),
            self.now(), body.get("segment_id"))
        return 201, {"segment_id": segment_id}

    def add_installation(self, ident, body):
        require_fields(body, ["segment_id", "instrument_id", "position",
                              "start_at", "end_at"])
        install_id = self.store.add_installation(
            body["segment_id"], body["instrument_id"], body["position"],
            body["start_at"], body["end_at"], self.now())
        return 201, {"install_id": install_id}

    # -- 读数接入 ----------------------------------------------------------

    def append_reading(self, ident, body):
        require_fields(body, ["instrument_id", "lot_id", "device_time",
                              "received_at", "raw_c"])
        if ident["role"] == "carrier":
            instrument = self.store.get_instrument(body["instrument_id"])
            if instrument is None or instrument["carrier_id"] != ident["party_id"]:
                raise ApiError(403, "承运商只能为登记在自己名下的设备上传读数")
        reading_id, stored, duplicate = self.store.append_reading(
            body["instrument_id"], body["lot_id"], body["device_time"],
            body["received_at"], float(body["raw_c"]),
            bool(body.get("backfill", False)),
            body.get("ingestion_id", "ingest-" + self.now()),
            body.get("segment_id"))
        return 200 if duplicate else 201, {
            "reading_id": reading_id, "stored": stored, "duplicate": duplicate,
            "device_time_preserved": body["device_time"],
            "received_at_preserved": body["received_at"],
        }

    def _carrier_segment_ids(self, party_id, lot_id=None):
        segs = self.store.segments_for_lot(lot_id) if lot_id else []
        return {s["segment_id"] for s in segs if s["carrier_id"] == party_id}

    def _owned_instrument_ids(self, party_id):
        """承运商隔离以设备归属为准：同一车厢里可能装着合作社自有记录仪，
        同车不等于承运商可见。"""
        return {r["instrument_id"] for r in self.store.conn.execute(
            "SELECT instrument_id FROM instruments WHERE carrier_id=?",
            (party_id,)).fetchall()}

    def list_readings(self, ident, lot_id):
        rows = self.store.readings_for_lot(
            lot_id, "9999-12-31T23:59:59+08:00")
        if ident["role"] == "carrier":
            own_instruments = self._owned_instrument_ids(ident["party_id"])
            rows = [r for r in rows if r["instrument_id"] in own_instruments]
        elif ident["role"] not in ("admin", "lab"):
            raise ApiError(403, "无权查看读数；理赔角色请使用脱敏证据包")
        return 200, {"lot_id": lot_id, "count": len(rows), "readings": rows}

    def my_segments(self, ident, body):
        all_segments = []
        for lot in self.store.all_lots():
            all_segments.extend(self.store.segments_for_lot(lot["lot_id"]))
        mine = [s for s in all_segments if s["carrier_id"] == ident["party_id"]]
        return 200, {"carrier_id": ident["party_id"], "segments": mine}

    def get_segment(self, ident, segment_id):
        seg = self.store.get_segment(segment_id)
        if seg is None:
            raise ApiError(404, "区段不存在")
        if ident["role"] == "carrier" and seg["carrier_id"] != ident["party_id"]:
            raise ApiError(403, "承运商只能查看自己负责的区段")
        if ident["role"] == "claims":
            raise ApiError(403, "理赔角色请通过脱敏证据包获取信息")
        return 200, seg

    # -- 判定 / 报告 -------------------------------------------------------

    def run_assessment(self, ident, lot_id, body):
        as_of = body.get("as_of") or self.now()
        result = logic.evaluate(self.store, lot_id, as_of, self.now(),
                                opts=body.get("opts"))
        return 200, result

    def latest_assessment(self, ident, lot_id):
        a = self.store.latest_assessment(lot_id)
        if a is None:
            raise ApiError(404, "该批次尚无判定")
        if ident["role"] == "carrier":
            own = self._owned_instrument_ids(ident["party_id"])
            lot_segments = self._carrier_segment_ids(ident["party_id"], lot_id)
            if not own or not (
                    {r["instrument_id"] for r in a["corrected_json"]} & own
                    or lot_segments):
                raise ApiError(403, "该批次没有归属于你的设备或区段")
            a = self._projection_for_carrier(a, own, lot_segments)
        elif ident["role"] not in ("admin", "lab"):
            raise ApiError(403, "无权查看判定")
        return 200, a

    @staticmethod
    def _projection_for_carrier(a, own_instruments, own_segments):
        keep_readings = {r["reading_id"] for r in a["corrected_json"]
                         if r["instrument_id"] in own_instruments}
        projection = dict(a)
        projection["corrected_json"] = [
            r for r in a["corrected_json"]
            if r["reading_id"] in keep_readings]
        projection["exclusions_json"] = [
            e for e in a["exclusions_json"]
            if e.get("reading_id") in keep_readings
            or e.get("instrument_id") in own_instruments
            or e.get("instrument_id") in own_segments]
        projection["scope"] = "carrier_projection"
        return projection

    def create_report(self, ident, lot_id, body):
        require_fields(body, ["as_of"])
        report_id, assessment_id, snapshot = logic.create_report(
            self.store, lot_id, body["as_of"], self.now(), opts=body.get("opts"))
        return 201, {"report_id": report_id, "assessment_id": assessment_id,
                     "snapshot_digest": snapshot["snapshot_digest"],
                     "verdict": snapshot["verdict"]}

    def get_report(self, ident, report_id):
        report = self.store.get_report(report_id)
        if report is None:
            raise ApiError(404, "报告不存在")
        report["snapshot"] = json.loads(report.pop("snapshot_json"))
        report["signatures"] = self.store.signatures(report_id)
        report["confirmations"] = self.store.confirmations(report_id)
        if ident["role"] == "carrier":
            own = self._owned_instrument_ids(ident["party_id"])
            own_segments = self._carrier_segment_ids(
                ident["party_id"], report["lot_id"])
            if not (
                    {r["instrument_id"] for r in
                     report["snapshot"]["corrected"]} & own
                    or own_segments):
                raise ApiError(403, "该报告涉及的批次没有归属于你的设备或区段")
            snap = report["snapshot"]
            snap["corrected"] = [r for r in snap["corrected"]
                                 if r["instrument_id"] in own]
            snap["exclusions"] = [
                e for e in snap["exclusions"]
                if e.get("instrument_id") in own
                or e.get("instrument_id") in own_segments]
            snap["scope"] = "carrier_projection"
        elif ident["role"] == "claims":
            raise ApiError(403, "理赔角色请通过脱敏证据包访问报告内容")
        return 200, report

    def sign_report(self, ident, report_id, body):
        require_fields(body, ["signed_by"])
        report = self.store.get_report(report_id)
        if report is None:
            raise ApiError(404, "报告不存在")
        sigs = logic.sign_report(self.store, report_id, body["signed_by"],
                                 self.now())
        return 201, {"report_id": report_id, "signatures": sigs,
                     "frozen": True}

    def confirm(self, ident, report_id, body):
        require_fields(body, ["decision"])
        if body["decision"] not in ("accepted", "disputed"):
            raise ApiError(400, "decision 只能是 accepted 或 disputed")
        report = self.store.get_report(report_id)
        if report is None:
            raise ApiError(404, "报告不存在")
        try:
            confirmation_id = self.store.add_confirmation(
                report_id, ident["party_id"], body["decision"],
                body.get("comment"), self.now())
        except Exception as exc:  # 同一方重复确认
            raise ApiError(409, f"该方已确认过报告: {exc}") from exc
        return 201, {"confirmation_id": confirmation_id,
                     "party": ident["party_id"], "decision": body["decision"]}

    def recompute(self, ident, lot_id, report_id):
        return 200, logic.recompute_dispute(self.store, lot_id, report_id)

    def make_package(self, ident, lot_id, report_id, body):
        package_id, redacted = logic.build_evidence_package(
            self.store, lot_id, report_id, self.now())
        return 201, {"package_id": package_id, "package": redacted}

    def get_package(self, ident, package_id):
        pkg = self.store.get_package(package_id)
        if pkg is None:
            raise ApiError(404, "证据包不存在")
        return 200, pkg

    # -- 覆盖率与计划 -------------------------------------------------------

    def coverage(self, ident, body):
        dimension = body.get("dimension", "route_id") if body else "route_id"
        try:
            return 200, {"dimension": dimension,
                         "groups": logic.compare_coverage(self.store, dimension)}
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc

    def generate_plans(self, ident, body):
        created = logic.generate_plans(self.store, self.now())
        return 201, {"created": [{"plan_id": p, "new": n} for p, n in created],
                     "open_plans": self.store.list_plans()}

    def list_plans(self, ident, body):
        return 200, {"plans": self.store.list_plans()}

    # -- 哈希链核验 ---------------------------------------------------------

    def chain(self, ident, instrument_id):
        ok, broken_at = self.store.verify_chain(instrument_id)
        return 200, {"instrument_id": instrument_id, "intact": ok,
                     "broken_at": broken_at}


def default_health():
    return {"status": "ok", "service": "flower-cold-chain",
            "name": "鲜切花冷链履约中枢"}


def make_server(store=None, port=8000, seed_dev_tokens=False, health=None):
    app = App(store=store, seed_dev_tokens=seed_dev_tokens)
    health = health or default_health

    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method):
            if method == "GET" and self.path.rstrip("/") == "/health":
                payload = health() if health else {"status": "ok"}
                status = 200
            else:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    status, payload = app.dispatch(method, self.path,
                                                   self.headers, raw)
                except ApiError as exc:
                    status, payload = exc.status, {"error": exc.message}
                except Exception as exc:  # 兜底：任何未预期错误返回结构化 500
                    status = 500
                    payload = {"error": f"服务器内部错误: {exc}"}
                    self.log_error("%s %s -> %s", method, self.path, exc)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def log_message(self, *_args):
            return

    return ThreadingHTTPServer(("0.0.0.0", port), Handler), app
