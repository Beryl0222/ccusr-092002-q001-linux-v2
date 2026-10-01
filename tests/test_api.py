"""HTTP 接口的端到端测试。"""

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from coldchain import AppendOnlyStore
from coldchain.api import make_handler
from service import health_payload
from support import CARRIER, LOT_ID, SEGMENT, WINDOW_END, WINDOW_START, ts


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = AppendOnlyStore()
        handler = make_handler(cls.store, health_payload)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_full_governance_flow(self):
        status, health = self.call("GET", "/health")
        self.assertEqual((status, health["service"]), (200, "flower-cold-chain"))

        status, _ = self.call("POST", "/lots", {
            "lot_id": LOT_ID, "cultivar": "红色切花月季",
            "harvested_at": "2026-09-14T05:40:00+08:00",
            "route_id": "KMG-BAV-D1", "packaging": "保湿棉+打孔膜",
            "temp_min_c": 2.0, "temp_max_c": 6.0,
        })
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", "/devices", {
            "device_id": "D1", "model": "M1", "owner_id": "coop-1",
            "owner_name": "某合作社", "serial": "SN-1",
            "install_position": "车厢前部", "tolerance_c": 0.5,
        })[0], 201)
        self.assertEqual(self.call("POST", "/calibrations", {
            "cert_id": "CERT-1", "device_id": "D1", "lab": "省计量院",
            "issued_at": "2026-01-01T00:00:00+08:00",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_until": "2027-01-01T00:00:00+08:00",
            "uncertainty_c": 0.2,
        })[0], 201)
        self.assertEqual(self.call("POST", "/bindings", {
            "device_id": "D1", "lot_id": LOT_ID, "segment_id": SEGMENT,
            "carrier_id": CARRIER, "started_at": WINDOW_START, "stopped_at": WINDOW_END,
        })[0], 201)
        for hour in (6, 7, 8):
            self.assertEqual(self.call("POST", "/readings", {
                "device_id": "D1", "lot_id": LOT_ID, "segment_id": SEGMENT,
                "device_time": ts(hour), "temperature_c": 4.0,
                "received_time": ts(hour),
            })[0], 201)

        # 非实验人员不得发布修正曲线
        self.assertEqual(self.call("POST", "/curves", {
            "actor_role": "carrier", "device_model": "M1", "points": [[0, 0]],
            "published_by": "x", "residual_uncertainty_c": 0.1,
        })[0], 403)

        status, report = self.call("POST", "/assessments", {
            "lot_id": LOT_ID, "segment_id": SEGMENT,
            "window_start": WINDOW_START, "window_end": WINDOW_END,
        })
        self.assertEqual(status, 201)
        self.assertEqual(report["verdict"], "indeterminate")  # 覆盖不足，不出结论

        for party in ("association", "carrier", "consignor"):
            self.assertEqual(
                self.call("POST", f"/reports/{report['report_id']}/confirm", {"party": party})[0],
                201,
            )
        status, state = self.call("GET", f"/reports/{report['report_id']}/status")
        self.assertTrue(state["signed"])

        status, package = self.call("GET", f"/reports/{report['report_id']}/evidence")
        self.assertEqual(status, 200)
        self.assertTrue(package["redacted"])
        self.assertNotIn("SN-1", json.dumps(package, ensure_ascii=False))

        status, recomputed = self.call("GET", f"/disputes/{report['report_id']}/recompute")
        self.assertTrue(recomputed["reproducible"])

        status, view = self.call("GET", f"/lots/{LOT_ID}/carrier-view?carrier_id={CARRIER}")
        self.assertEqual([s["segment_id"] for s in view["segments"]], [SEGMENT])
        self.assertEqual(self.call("GET", "/analytics/coverage")[0], 200)
        self.assertEqual(self.call("GET", "/maintenance/plan")[0], 200)


if __name__ == "__main__":
    unittest.main()
