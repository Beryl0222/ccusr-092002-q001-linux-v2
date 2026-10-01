"""冷链测量治理规则测试。"""

import json
import unittest

from coldchain import (
    PermissionDenied,
    amend_report,
    assess_lot,
    carrier_view,
    claims_evidence_package,
    confirm_report,
    coverage_report,
    effective_readings,
    ingest_reading,
    maintenance_plan,
    publish_curve,
    raw_readings,
    recompute_report,
    register_drift,
    report_status,
    save_report,
)
from coldchain.model import ClockDrift
from support import (
    CARRIER,
    LOT_ID,
    SEGMENT,
    WINDOW_END,
    WINDOW_START,
    build_store,
    fill_readings,
    ts,
)


def make_report(store, **kwargs):
    return save_report(store, assess_lot(store, LOT_ID, SEGMENT, WINDOW_START, WINDOW_END, **kwargs))


def sign_all(store, report_id):
    for party in ("association", "carrier", "consignor"):
        confirm_report(store, report_id, party)


class AppendOnlyTest(unittest.TestCase):
    def test_duplicate_ingest_never_overwrites(self):
        store = build_store()
        ingest_reading(store, device_id="D1", lot_id=LOT_ID, segment_id=SEGMENT,
                       device_time=ts(8), temperature_c=4.0, received_time=ts(8))
        ingest_reading(store, device_id="D1", lot_id=LOT_ID, segment_id=SEGMENT,
                       device_time=ts(8), temperature_c=4.6, received_time=ts(8, 30))
        self.assertEqual(len(raw_readings(store, LOT_ID)), 2)  # 原始记录都保留
        effective = effective_readings(store, LOT_ID)
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["temperature_c"], 4.6)  # 生效视图取最新

    def test_late_upload_keeps_both_timestamps(self):
        store = build_store()
        ingest_reading(store, device_id="D1", lot_id=LOT_ID, segment_id=SEGMENT,
                       device_time=ts(8), temperature_c=4.0, received_time=ts(23))
        record = raw_readings(store, LOT_ID)[0]
        self.assertTrue(record["late"])
        self.assertEqual(record["device_time"], ts(8))
        self.assertEqual(record["received_time"], ts(23))


class QualityGateTest(unittest.TestCase):
    def test_compliant_when_all_gates_pass(self):
        store = build_store()
        fill_readings(store, temp=4.0)
        report = make_report(store)
        self.assertEqual(report["verdict"], "compliant")
        self.assertTrue(all(g["passed"] for g in report["gates"]))

    def test_expired_calibration_blocks_conclusion(self):
        store = build_store(cert_valid_until="2026-09-01T00:00:00+08:00")
        fill_readings(store)
        report = make_report(store)
        self.assertEqual(report["verdict"], "indeterminate")
        self.assertTrue(report["exclusions"])
        self.assertEqual(report["exclusions"][0]["reason"], "校准证书过期")
        self.assertEqual(report["used_reading_ids"], [])

    def test_sampling_gap_blocks_conclusion(self):
        store = build_store()
        for hour in (6, 11):  # 中间 5 小时无数据
            ingest_reading(store, device_id="D1", lot_id=LOT_ID, segment_id=SEGMENT,
                           device_time=ts(hour), temperature_c=4.0, received_time=ts(hour))
        report = make_report(store)
        self.assertEqual(report["verdict"], "indeterminate")
        failed = {g["gate"] for g in report["gates"] if not g["passed"]}
        self.assertIn("采样缺口", failed)

    def test_probe_deviation_blocks_conclusion(self):
        store = build_store(with_devices=("D1", "D2"))
        fill_readings(store, "D1", temp=4.0)
        fill_readings(store, "D2", temp=7.5)  # 两探头系统性相差 3.5℃
        report = make_report(store)
        self.assertEqual(report["verdict"], "indeterminate")
        failed = {g["gate"] for g in report["gates"] if not g["passed"]}
        self.assertIn("探头间偏差", failed)

    def test_uncertainty_interval_crossing_limit_is_indeterminate(self):
        store = build_store()
        fill_readings(store, temp=5.2)  # 峰值未越限，但扩展不确定区间跨限
        report = make_report(store)
        self.assertEqual(report["verdict"], "indeterminate")
        self.assertGreater(report["interval_c"][1], 6.0)

    def test_clearly_warm_is_non_compliant(self):
        store = build_store()
        fill_readings(store, temp=8.0)  # 扣除不确定度仍越限
        report = make_report(store)
        self.assertEqual(report["verdict"], "non_compliant")

    def test_outside_active_interval_not_assessed(self):
        store = build_store()
        fill_readings(store)
        ingest_reading(store, device_id="D1", lot_id=LOT_ID, segment_id=SEGMENT,
                       device_time=ts(13), temperature_c=25.0, received_time=ts(13))
        report = make_report(store)  # 启停区间外读数不参与评估
        self.assertEqual(report["verdict"], "compliant")

    def test_clock_drift_recorded_in_report(self):
        store = build_store()
        register_drift(store, ClockDrift(device_id="D1", measured_at=ts(5), offset_seconds=300.0))
        fill_readings(store)
        report = make_report(store)
        self.assertEqual(report["drift_seconds"], {"D1": 300.0})


class CorrectionCurveTest(unittest.TestCase):
    def test_only_lab_publishes_curve(self):
        store = build_store()
        with self.assertRaises(PermissionDenied):
            publish_curve(store, actor_role="carrier", device_model="M1",
                         points=[[0, 0], [10, 0]], published_by="x", residual_uncertainty_c=0.1)

    def test_new_curve_version_does_not_alter_signed_report(self):
        store = build_store()
        fill_readings(store, temp=4.0)
        publish_curve(store, actor_role="lab", device_model="M1",
                     points=[[0, 0.0], [10, 0.0]], published_by="实验员甲",
                     residual_uncertainty_c=0.1)
        report = make_report(store)
        self.assertEqual(report["curve_versions"], {"M1": 1})
        sign_all(store, report["report_id"])
        # 新版本曲线把读数大幅下修，但已签署报告保持原样
        publish_curve(store, actor_role="lab", device_model="M1",
                     points=[[0, -3.0], [10, -3.0]], published_by="实验员乙",
                     residual_uncertainty_c=0.1)
        result = recompute_report(store, report["report_id"])
        self.assertTrue(result["reproducible"])  # 复算仍按 v1
        self.assertEqual(result["verdict"], "compliant")

    def test_amendment_creates_linked_new_version(self):
        store = build_store()
        fill_readings(store, temp=4.0)
        report = make_report(store)
        sign_all(store, report["report_id"])
        publish_curve(store, actor_role="lab", device_model="M1",
                     points=[[0, -3.0], [10, -3.0]], published_by="实验员乙",
                     residual_uncertainty_c=0.1)
        amended = amend_report(store, report["report_id"], reason="发布修正曲线 v1")
        self.assertEqual(amended["version"], 2)
        self.assertEqual(amended["supersedes"], report["digest"])
        self.assertEqual(amended["curve_versions"], {"M1": 1})
        # 原报告未被修改
        self.assertEqual(report_status(store, report["report_id"])["digest"], report["digest"])

    def test_unsigned_report_cannot_be_amended(self):
        store = build_store()
        fill_readings(store)
        report = make_report(store)
        with self.assertRaises(ValueError):
            amend_report(store, report["report_id"], reason="尚未签署")


class SigningTest(unittest.TestCase):
    def test_signed_only_after_all_parties_confirm(self):
        store = build_store()
        fill_readings(store)
        report = make_report(store)
        confirm_report(store, report["report_id"], "association")
        self.assertFalse(report_status(store, report["report_id"])["signed"])
        confirm_report(store, report["report_id"], "carrier")
        confirm_report(store, report["report_id"], "consignor")
        status = report_status(store, report["report_id"])
        self.assertTrue(status["signed"])
        self.assertEqual(status["pending_parties"], [])

    def test_duplicate_confirmation_rejected(self):
        store = build_store()
        fill_readings(store)
        report = make_report(store)
        confirm_report(store, report["report_id"], "carrier")
        with self.assertRaises(ValueError):
            confirm_report(store, report["report_id"], "carrier")


class AccessTest(unittest.TestCase):
    def test_carrier_sees_only_own_segment(self):
        store = build_store()
        fill_readings(store)
        view = carrier_view(store, CARRIER, LOT_ID)
        self.assertEqual([s["segment_id"] for s in view["segments"]], [SEGMENT])
        other = carrier_view(store, "carrier-other", LOT_ID)
        self.assertEqual(other["segments"], [])

    def test_claims_package_is_redacted(self):
        store = build_store()
        fill_readings(store)
        report = make_report(store)
        sign_all(store, report["report_id"])
        package = claims_evidence_package(store, report["report_id"])
        blob = json.dumps(package, ensure_ascii=False)
        self.assertNotIn("SN-D1-0001", blob)  # 序列号脱敏
        self.assertNotIn("玉溪某合作社", blob)  # 所有人名称脱敏
        self.assertTrue(package["redacted"])
        self.assertTrue(package["signed"])
        self.assertEqual(len(package["confirmations"]), 3)
        self.assertTrue(package["readings"])


class AnalyticsTest(unittest.TestCase):
    def test_coverage_grouped_by_route_packaging_season(self):
        store = build_store()
        fill_readings(store)
        rows = coverage_report(store)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["route_id"], "KMG-BAV-D1")
        self.assertEqual(row["packaging"], "保湿棉+打孔膜")
        self.assertEqual(row["season"], "秋")
        self.assertGreater(row["avg_coverage"], 0.9)

    def test_maintenance_plan_flags_expiring_and_unreliable_devices(self):
        store = build_store(cert_valid_until="2026-09-01T00:00:00+08:00")
        fill_readings(store)
        make_report(store)  # 证书在读数时已过期，读数全部被排除
        tasks = maintenance_plan(store, as_of="2026-09-10T00:00:00+08:00", horizon_days=30)
        kinds = {(t["device_id"], t["type"]) for t in tasks}
        self.assertIn(("D1", "复校"), kinds)
        self.assertIn(("D1", "轮换"), kinds)  # 排除率 100%


class DisputeTest(unittest.TestCase):
    def test_recompute_reproduces_signed_report(self):
        store = build_store()
        fill_readings(store)
        report = make_report(store)
        sign_all(store, report["report_id"])
        result = recompute_report(store, report["report_id"])
        self.assertTrue(result["reproducible"])
        self.assertEqual(result["differences"], [])
        self.assertEqual(len(result["confirmations"]), 3)
        self.assertTrue(result["used_reading_ids"])
        self.assertIsNotNone(result["interval_c"])

    def test_recompute_detects_post_signing_upload(self):
        store = build_store()
        fill_readings(store, temp=4.0)
        report = make_report(store)
        sign_all(store, report["report_id"])
        # 签署后补传一条同一设备同一时刻的不同读数
        ingest_reading(store, device_id="D1", lot_id=LOT_ID, segment_id=SEGMENT,
                       device_time=ts(8), temperature_c=25.0, received_time=ts(20))
        result = recompute_report(store, report["report_id"])
        self.assertFalse(result["reproducible"])
        changed = {d["field"] for d in result["differences"]}
        self.assertIn("used_reading_ids", changed)


if __name__ == "__main__":
    unittest.main()
