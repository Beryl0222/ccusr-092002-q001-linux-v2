"""HTTP 接口测试：角色边界、区段隔离、端到端争议处置。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

from coldchain.api import App, make_server
from coldchain.store import Store

NOW = "2026-09-14T13:05:00+08:00"
T0 = datetime.fromisoformat("2026-09-14T06:00:00+08:00")
LOT = "YX-20260914-ROSE-01"


def ts(m):
    return (T0 + timedelta(minutes=m)).isoformat()


TOKENS = {
    "admin": "dev-admin-token",
    "lab": "dev-lab-token",
    "carrier": "dev-carrier-token",
    "claims": "dev-claims-token",
}


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.app = make_server(
            store=Store(":memory:"), port=0, seed_dev_tokens=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)

    def call(self, method, path, role=None, payload=None, expect=None):
        headers = {"Content-Type": "application/json"}
        if role:
            headers["Authorization"] = f"Bearer {TOKENS[role]}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") \
            if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            status = exc.code
        if expect is not None:
            self.assertEqual(status, expect, body)
        return status, body

    def setUp(self):
        # 每个用例使用独立批次 / 区段，避免共享库相互污染；设备全局登记一次。
        self.suffix = uuid.uuid4().hex[:8]
        self.lot = f"{LOT}-{self.suffix}"
        self.seg_trunk = f"SEG-TRUNK-{self.suffix}"
        self.seg_cold = f"SEG-COLD-{self.suffix}"

    @property
    def instruments_seeded(self):
        return getattr(type(self), "_instruments_seeded", False)

    def _seed_world(self, coop_temp=4.5, car_temp=4.7, car_id="CARRIER-YX-01"):
        self.call("POST", "/api/lots", "admin", {
            "lot_id": self.lot, "cultivar": "红色切花月季",
            "route_id": "KUN-BANG-R2", "packaging": "泡沫箱",
            "season": "autumn", "harvested_at": ts(-20), "stems": 2400})
        if not self.instruments_seeded:
            for ins, model in (("INS-COOP", "CoopLogger"),
                               ("INS-CAR", "CarProbe"),
                               ("INS-COLD", "ColdProbe")):
                self.call("POST", "/api/instruments", "admin", {
                    "instrument_id": ins, "model": model,
                    "kind": "probe",
                    "carrier_id": car_id if ins == "INS-CAR" else None})
                self.call("POST", "/api/calibrations", "lab", {
                    "instrument_id": ins, "lab": "LAB-YN-01",
                    "valid_from": "2026-03-01T00:00:00+08:00",
                    "valid_until": "2027-03-01T00:00:00+08:00",
                    "max_abs_error_c": 0.3})
                self.call("POST", "/api/clock-drifts", "lab", {
                    "instrument_id": ins,
                    "checked_at": "2026-09-14T05:30:00+08:00",
                    "device_clock_at": "2026-09-14T05:30:00+08:00",
                    "ppm": 0})
            type(self)._instruments_seeded = True
        self.call("POST", "/api/segments", "admin", {
            "segment_id": self.seg_trunk, "lot_id": self.lot,
            "carrier_id": car_id, "name": "昆明-呈贡干线",
            "start_at": ts(0), "end_at": ts(120),
            "limit_min_c": 2.0, "limit_max_c": 8.0,
            "nominal_interval_s": 300})
        self.call("POST", "/api/segments", "admin", {
            "segment_id": self.seg_cold, "lot_id": self.lot,
            "carrier_id": "COLDSTORE-01", "name": "呈贡冷库",
            "start_at": ts(120), "end_at": ts(300),
            "limit_min_c": 2.0, "limit_max_c": 8.0,
            "nominal_interval_s": 300})
        self.call("POST", "/api/installations", "admin", {
            "segment_id": self.seg_trunk, "instrument_id": "INS-COOP",
            "position": "货箱中部", "start_at": ts(0), "end_at": ts(120)})
        self.call("POST", "/api/installations", "admin", {
            "segment_id": self.seg_trunk, "instrument_id": "INS-CAR",
            "position": "车厢侧壁", "start_at": ts(0), "end_at": ts(120)})
        self.call("POST", "/api/installations", "admin", {
            "segment_id": self.seg_cold, "instrument_id": "INS-COLD",
            "position": "货架回风", "start_at": ts(120), "end_at": ts(300)})
        for m in range(0, 121, 5):
            self.call("POST", "/api/readings", "admin", {
                "instrument_id": "INS-COOP", "lot_id": self.lot,
                "device_time": ts(m), "received_at": ts(m + 1),
                "raw_c": coop_temp, "ingestion_id": f"coop-{m}-{self.suffix}"})
            self.call("POST", "/api/readings", "carrier", {
                "instrument_id": "INS-CAR", "lot_id": self.lot,
                "device_time": ts(m), "received_at": ts(m + 1),
                "raw_c": car_temp, "ingestion_id": f"car-{m}-{self.suffix}"})
        for m in range(120, 301, 5):
            self.call("POST", "/api/readings", "admin", {
                "instrument_id": "INS-COLD", "lot_id": self.lot,
                "device_time": ts(m), "received_at": ts(m + 1),
                "raw_c": 3.8, "ingestion_id": f"cold-{m}-{self.suffix}"})

    # -- 基础与鉴权 --------------------------------------------------------

    def test_health_open(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "flower-cold-chain")

    def test_missing_and_bad_token_rejected(self):
        status, body = self.call("GET", "/api/plans")
        self.assertEqual(status, 401)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/plans",
            headers={"Authorization": "Bearer nope"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 401)

    def test_role_boundaries(self):
        # 理赔不能发曲线、不能看全量报告。
        self.call("POST", "/api/correction-curves", "claims",
                  {"kind": "polynomial", "params": {"coeffs": [0, 1]},
                   "valid_from": NOW, "instrument_id": "INS-COOP"},
                  expect=403)
        # 承运商不能发修正曲线。
        self.call("POST", "/api/correction-curves", "carrier",
                  {"kind": "polynomial", "params": {"coeffs": [0, 1]},
                   "valid_from": NOW, "instrument_id": "INS-CAR"},
                  expect=403)
        # 实验室可以发布曲线。
        status, body = self.call("POST", "/api/correction-curves", "lab", {
            "instrument_model": "CoopLogger", "kind": "polynomial",
            "params": {"coeffs": [0, 1], "uncertainty_c": 0.1},
            "valid_from": "2026-09-01T00:00:00+08:00",
            "note": "标准温箱首轮修正"})
        self.assertEqual(status, 201)
        self.assertEqual(body["version"], 1)

    # -- 区段隔离 ----------------------------------------------------------

    def test_carrier_only_owns_segments_and_instruments(self):
        self._seed_world()
        status, mine = self.call("GET", "/api/my/segments", "carrier")
        self.assertEqual(status, 200)
        ids = {s["segment_id"] for s in mine["segments"]}
        self.assertIn(self.seg_trunk, ids)
        self.assertNotIn(self.seg_cold, ids)
        # 看不到冷库区段。
        self.call("GET", f"/api/segments/{self.seg_cold}", "carrier",
                  expect=403)
        self.call("GET", f"/api/segments/{self.seg_trunk}", "carrier",
                  expect=200)
        # 不能为别人名下的设备上传。
        self.call("POST", "/api/readings", "carrier", {
            "instrument_id": "INS-COOP", "lot_id": self.lot,
            "device_time": ts(60), "received_at": NOW, "raw_c": 12.0,
            "ingestion_id": "spoof"}, expect=403)
        # 读数列表里只有自己设备的数据。
        status, rows = self.call(
            "GET", f"/api/lots/{self.lot}/readings", "carrier")
        self.assertEqual({r["instrument_id"] for r in rows["readings"]},
                         {"INS-CAR"})

    def test_carrier_self_registers_own_instrument(self):
        suffix = uuid.uuid4().hex[:8]
        ins = f"INS-SELF-{suffix}"
        # 承运商可登记设备，身份强制绑定为自己；伪造他人 carrier_id 无效。
        status, body = self.call("POST", "/api/instruments", "carrier", {
            "instrument_id": ins, "model": "SelfLogger", "kind": "recorder",
            "carrier_id": "SOMEONE-ELSE"})
        self.assertEqual(status, 201)
        row = self.app.store.get_instrument(ins)
        self.assertEqual(row["carrier_id"], "CARRIER-YX-01")
        # 补齐证书，避免该全局共享设备影响后续计划生成断言。
        self.call("POST", "/api/calibrations", "lab", {
            "instrument_id": ins, "lab": "LAB-YN-01",
            "valid_from": "2026-03-01T00:00:00+08:00",
            "valid_until": "2027-03-01T00:00:00+08:00",
            "max_abs_error_c": 0.3})
        self.call("POST", "/api/clock-drifts", "lab", {
            "instrument_id": ins,
            "checked_at": "2026-09-14T05:30:00+08:00",
            "device_clock_at": "2026-09-14T05:30:00+08:00", "ppm": 0})

    def test_carrier_uploads_for_own_instrument(self):
        self._seed_world()
        status, body = self.call("POST", "/api/readings", "carrier", {
            "instrument_id": "INS-CAR", "lot_id": self.lot,
            "device_time": ts(130), "received_at": NOW, "raw_c": 5.0,
            "ingestion_id": "own-upload"})
        # 130 分钟已超出 INS-CAR 安装窗口，但上传始终允许（原始留存），
        # 是否采用由判定门限决定。
        self.assertEqual(status, 201)
        self.assertTrue(body["stored"])

    # -- 端到端：矛盾读数 -> 报告 -> 签署 -> 补传 -> 复算 -> 脱敏包 --------

    def test_disputed_lot_end_to_end(self):
        # 合作社 4.5℃ 与承运商 10.5℃ 互相矛盾。
        self._seed_world(coop_temp=4.5, car_temp=10.5)

        status, assessment = self.call(
            "POST", f"/api/lots/{self.lot}/assessments", "admin",
            {"as_of": "2026-09-14T13:00:00+08:00"})
        self.assertEqual(status, 200)
        self.assertEqual(assessment["verdict"], "inconclusive")
        self.assertIn("probe_divergence", assessment["summary"]["blockers"])

        # 保险查勘拿脱敏包之前报告尚不存在。
        self.call("GET", "/api/packages/PKG_x", "claims", expect=404)

        status, created = self.call(
            "POST", f"/api/lots/{self.lot}/reports", "admin",
            {"as_of": "2026-09-14T13:00:00+08:00"})
        self.assertEqual(status, 201)
        report_id = created["report_id"]
        self.assertEqual(created["verdict"], "inconclusive")

        # 理赔不能直接看原始报告。
        self.call("GET", f"/api/reports/{report_id}", "claims", expect=403)

        # 各方确认：承运商用自己的令牌表达争议（party 取令牌绑定身份）。
        self.call("POST", f"/api/reports/{report_id}/confirmations",
                  "carrier", {"decision": "disputed",
                              "comment": "侧壁探头被日晒"}, expect=201)
        # 同一方重复确认被拒。
        self.call("POST", f"/api/reports/{report_id}/confirmations",
                  "carrier", {"decision": "accepted"}, expect=409)

        # 协会签署冻结。
        status, signed = self.call("POST",
            f"/api/reports/{report_id}/sign", "admin",
            {"signed_by": "ASSOCIATION"})
        self.assertEqual(status, 201)
        self.assertTrue(signed["frozen"])

        # 签署后补传一整段“合格”数据也无法改写结论。
        for m in range(0, 121, 5):
            self.call("POST", "/api/readings", "admin", {
                "instrument_id": "INS-CAR", "lot_id": self.lot,
                "device_time": ts(m), "received_at":
                "2026-09-16T10:00:00+08:00", "raw_c": 4.6,
                "backfill": True, "ingestion_id": f"late-{m}-{self.suffix}"})

        status, rec = self.call(
            "GET", f"/api/lots/{self.lot}/reports/{report_id}/recompute",
            "admin")
        self.assertEqual(status, 200)
        self.assertTrue(rec["recompute_matches"])
        self.assertTrue(rec["snapshot_digest_intact"])
        self.assertEqual(rec["verdict_snapshot"], "inconclusive")
        self.assertTrue(any(c["decision"] == "disputed"
                            for c in rec["confirmations"]))

        # 理赔获得脱敏证据包。
        status, pkg_resp = self.call(
            "POST", f"/api/lots/{self.lot}/reports/{report_id}"
                    "/evidence-package", "claims", {})
        self.assertEqual(status, 201)
        pkg = pkg_resp["package"]
        raw = json.dumps(pkg, ensure_ascii=False)
        for secret in ("INS-COOP", "INS-CAR", "CARRIER-YX-01", "LAB-YN-01",
                       "CoopLogger", "CarProbe"):
            self.assertNotIn(secret, raw)
        self.assertTrue(pkg["readings"])
        self.assertIn("probe_coverage", pkg)

        # 证据包可凭 id 取回。
        status, fetched = self.call(
            "GET", f"/api/packages/{pkg_resp['package_id']}", "claims")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["redacted_json"]["report_digest"],
                         pkg["report_digest"])

    def test_clear_excursion_reaches_exceeded_verdict(self):
        self._seed_world(coop_temp=10.5, car_temp=10.6)
        status, created = self.call(
            "POST", f"/api/lots/{self.lot}/reports", "admin",
            {"as_of": "2026-09-14T13:00:00+08:00"})
        self.assertEqual(created["verdict"], "exceeded")

    # -- 覆盖率与计划 ------------------------------------------------------

    def test_coverage_and_plans(self):
        self._seed_world()
        status, cov = self.call("GET", "/api/coverage", "admin")
        self.assertEqual(status, 200)
        self.assertTrue(cov["groups"])

        status, body = self.call("POST", "/api/plans/generate", "admin", {})
        self.assertEqual(status, 201)
        # 设备证书均有效、无偏差，不应产生复校计划。
        recal = [p for p in body["open_plans"]
                 if p["kind"] == "recalibration"]
        self.assertEqual(recal, [])

        # 登记一台从未校准的新设备并再次生成。
        self.call("POST", "/api/instruments", "admin",
                  {"instrument_id": "INS-NEW", "model": "CheapLogger",
                   "kind": "recorder"})
        status, body = self.call("POST", "/api/plans/generate", "admin", {})
        self.assertTrue(any(p["target_id"] == "INS-NEW"
                            for p in body["open_plans"]))
        # 实验室可只读查看计划。
        self.call("GET", "/api/plans", "lab", expect=200)
        # 承运商无权。
        self.call("GET", "/api/plans", "carrier", expect=403)

    def test_hash_chain_endpoint(self):
        self._seed_world()
        status, body = self.call(
            "GET", "/api/instruments/INS-CAR/chain", "lab")
        self.assertEqual(status, 200)
        self.assertTrue(body["intact"])
        self.call("GET", "/api/instruments/INS-CAR/chain", "claims",
                  expect=403)


if __name__ == "__main__":
    unittest.main()
