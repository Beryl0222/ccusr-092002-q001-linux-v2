"""判定逻辑测试：门限、覆盖率、偏差、冻结复算、覆盖率与计划、脱敏。"""

import unittest
from datetime import datetime, timedelta

from coldchain import logic
from coldchain.store import Store

T0 = datetime.fromisoformat("2026-09-14T06:00:00+08:00")
AS_OF = "2026-09-14T13:00:00+08:00"
NOW = "2026-09-14T13:05:00+08:00"


def ts(offset_min):
    return (T0 + timedelta(minutes=offset_min)).isoformat()


class Scenario:
    """构造一个双探头、双区段、校准齐全的标准争议场景。"""

    def __init__(self, interval_s=300, limit_max=8.0, limit_min=2.0):
        self.s = Store()
        self.now = NOW
        self.s.add_instrument("CoopLogger-A1", "recorder",
                              now=NOW, instrument_id="INS-COOP")
        self.s.add_instrument("CarrierProbe-B2", "probe",
                              carrier_id="CARRIER-YX-01",
                              now=NOW, instrument_id="INS-CAR")
        self.s.add_instrument("ColdStore-C3", "probe",
                              now=NOW, instrument_id="INS-COLD")
        self.s.add_lot("YX-20260914-ROSE-01", cultivar="红色切花月季",
                       route_id="KUN-BANG-R2", packaging="泡沫箱",
                       season="autumn", harvested_at=ts(-20), stems=2400, now=NOW)
        for ins, err in (("INS-COOP", 0.3), ("INS-CAR", 0.25), ("INS-COLD", 0.2)):
            self.s.add_calibration(
                ins, "LAB-YN-01", "2026-03-01T00:00:00+08:00",
                "2027-03-01T00:00:00+08:00", err, NOW, cert_id=f"CERT-{ins}")
            # 时钟核查：设备时钟与真实时间一致，ppm=0。
            self.s.add_clock_drift(
                ins, "2026-09-14T05:30:00+08:00",
                "2026-09-14T05:30:00+08:00", 0.0, NOW, drift_id=f"D-{ins}")
        self.s.add_segment(
            "YX-20260914-ROSE-01", "CARRIER-YX-01", "昆明-呈贡干线",
            ts(0), ts(120), limit_min, limit_max, interval_s, NOW,
            segment_id="SEG-TRUNK")
        self.s.add_segment(
            "YX-20260914-ROSE-01", "COLDSTORE-01", "呈贡冷库",
            ts(120), ts(300), limit_min, limit_max, interval_s, NOW,
            segment_id="SEG-COLD")
        self.s.add_installation("SEG-TRUNK", "INS-COOP", "货箱中部",
                                ts(0), ts(120), NOW, install_id="I1")
        self.s.add_installation("SEG-TRUNK", "INS-CAR", "车厢侧壁",
                                ts(0), ts(120), NOW, install_id="I2")
        self.s.add_installation("SEG-COLD", "INS-COLD", "货架回风处",
                                ts(120), ts(300), NOW, install_id="I3")
        self.interval_s = interval_s

    def feed(self, instrument, start_min, end_min, temp_fn, backfill=False,
             ingestion="ing-1", skip=()):
        step = self.interval_s // 60
        m = start_min
        while m <= end_min:
            if m not in skip:
                t = ts(m)
                self.s.append_reading(
                    instrument, "YX-20260914-ROSE-01", t, t,
                    float(temp_fn(m)), backfill=backfill,
                    ingestion_id=ingestion, segment_id=None)
            m += step


class ComplianceTest(unittest.TestCase):
    def setUp(self):
        self.sc = Scenario()

    def test_all_compliant_when_aligned(self):
        sc = self.sc
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        sc.feed("INS-CAR", 0, 120, lambda m: 4.7)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertEqual(r["verdict"], "compliant")
        self.assertGreaterEqual(r["summary"]["effective_coverage"], 0.95)
        self.assertEqual(r["summary"]["exceeded_points"], 0)

    def test_conflicting_loggers_divergence_is_inconclusive(self):
        """合作社记录仪合格 vs 承运商探头超温：偏差无法被不确定度解释时，
        不得直接判定超温，只能 inconclusive。"""
        sc = self.sc
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        sc.feed("INS-CAR", 0, 120, lambda m: 10.5)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertEqual(r["verdict"], "inconclusive")
        self.assertIn("probe_divergence", r["summary"]["blockers"])
        codes = {e["code"] for e in r["exclusions"]}
        self.assertIn("probe_divergence", codes)

    def test_small_divergence_within_uncertainty_is_compliant(self):
        sc = self.sc
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        # 0.4℃ 偏差，扩展不确定区间重叠，不构成 divergence。
        sc.feed("INS-CAR", 0, 120, lambda m: 4.9)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertEqual(r["verdict"], "compliant")

    def test_calibration_expired_blocks_conclusion(self):
        sc = self.sc
        # 把 INS-CAR 证书改成已过期。
        sc.s.conn.execute(
            "UPDATE calibration_certs SET valid_until=? WHERE cert_id=?",
            ("2026-09-01T00:00:00+08:00", "CERT-INS-CAR"))
        sc.s.conn.commit()
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        sc.feed("INS-CAR", 0, 120, lambda m: 12.0)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertEqual(r["verdict"], "inconclusive")
        self.assertIn("calibration_expired", r["summary"]["blockers"])
        excluded = {x["reading_id"] for x in r["corrected"] if not x["usable"]}
        self.assertTrue(excluded)

    def test_missing_clock_check_blocks_conclusion(self):
        sc = self.sc
        sc.s.conn.execute("DELETE FROM clock_drifts WHERE instrument_id=?",
                          ("INS-COOP",))
        sc.s.conn.commit()
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        sc.feed("INS-CAR", 0, 120, lambda m: 4.6)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertEqual(r["verdict"], "inconclusive")
        self.assertIn("clock_unverified", r["summary"]["blockers"])

    def test_sampling_gap_lowers_coverage(self):
        sc = Scenario(interval_s=300)
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        # 承运商探头在 30~75 分钟整段缺失（缺口 45 分钟）。
        sc.feed("INS-CAR", 0, 120, lambda m: 4.7,
                skip={30, 35, 40, 45, 50, 55, 60, 65, 70, 75})
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertEqual(r["verdict"], "inconclusive")
        self.assertIn("insufficient_coverage", r["summary"]["blockers"])
        self.assertIn("sampling_gap", {e["code"] for e in r["exclusions"]})

    def test_clear_excursion_with_consistent_probes_is_exceeded(self):
        """多支探头一致超温才能判 exceeded。"""
        sc = self.sc
        sc.feed("INS-COOP", 0, 120,
                lambda m: 10.5 if 40 <= m <= 80 else 4.5)
        sc.feed("INS-CAR", 0, 120,
                lambda m: 10.6 if 40 <= m <= 80 else 4.6)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertEqual(r["verdict"], "exceeded")
        self.assertGreater(r["summary"]["exceeded_points"], 0)

    def test_marginal_limit_crossing_is_inconclusive(self):
        """限值恰落在扩展不确定区间内时不能判合格也不能判超温。"""
        sc = self.sc
        # 两支探头在 40~50 分钟同时贴近 8℃ 上限，保持互相一致以免触发偏差门限。
        sc.feed("INS-COOP", 0, 120,
                lambda m: 7.8 if 40 <= m <= 50 else 4.5)
        sc.feed("INS-CAR", 0, 120,
                lambda m: 7.95 if 40 <= m <= 50 else 4.7)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertEqual(r["verdict"], "inconclusive")
        self.assertGreater(r["summary"]["marginal_points"], 0)

    def test_backfill_dual_timestamps_preserved(self):
        sc = self.sc
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        sc.feed("INS-CAR", 0, 120, lambda m: 4.7)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        # 补传：设备时间在运输窗口内，接收时间晚一天。
        sc.s.append_reading(
            "INS-CAR", "YX-20260914-ROSE-01", ts(60),
            "2026-09-15T20:00:00+08:00", 4.8, backfill=True,
            ingestion_id="late-batch")
        rows = sc.s.readings_for_lot("YX-20260914-ROSE-01",
                                     "2026-09-20T00:00:00+08:00")
        late = [r for r in rows if r["backfill"]]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["device_time"], ts(60))
        self.assertNotEqual(late[0]["device_time"], late[0]["received_at"])

    def test_conflicting_retransmit_keeps_both_rows_uses_earliest(self):
        sc = self.sc
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        sc.feed("INS-CAR", 0, 120, lambda m: 4.7)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        # 同设备同设备时间、不同值、更晚接收（换 ingestion_id 绕过幂等）。
        sc.s.append_reading(
            "INS-CAR", "YX-20260914-ROSE-01", ts(60),
            "2026-09-15T20:00:00+08:00", 12.0,
            ingestion_id="conflict-batch")
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01",
                           "2026-09-20T00:00:00+08:00", NOW)
        # 12℃ 的后到行不得制造超温结论。
        self.assertNotEqual(r["verdict"], "exceeded")
        self.assertIn("duplicate_timestamp",
                      {e["code"] for e in r["exclusions"]})


class CorrectionCurveTest(unittest.TestCase):
    def setUp(self):
        self.sc = Scenario()
        sc = self.sc
        sc.feed("INS-COOP", 0, 120, lambda m: 4.0)
        sc.feed("INS-CAR", 0, 120, lambda m: 4.0)
        sc.feed("INS-COLD", 120, 300, lambda m: 4.0)

    def test_versioned_curve_applied(self):
        sc = self.sc
        sc.s.add_curve(
            "INS-COOP", None, "polynomial",
            '{"coeffs": [0.5, 1.0], "uncertainty_c": 0.1}',
            "2026-09-01T00:00:00+08:00", "LAB-YN-01", NOW)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        coop = [x for x in r["corrected"] if x["instrument_id"] == "INS-COOP"]
        self.assertTrue(coop)
        self.assertAlmostEqual(coop[0]["corrected_c"], 4.5, places=3)
        self.assertEqual(coop[0]["curve_version"], 1)

    def test_new_curve_version_does_not_change_signed_report(self):
        """实验人员事后发布 v2 修正曲线，已签署报告复算仍采用 v1。"""
        sc = self.sc
        sc.s.add_curve(
            "INS-COOP", None, "polynomial",
            '{"coeffs": [0.5, 1.0], "uncertainty_c": 0.1}',
            "2026-09-01T00:00:00+08:00", "LAB-YN-01", NOW)
        report_id, _, _ = logic.create_report(
            sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        logic.sign_report(sc.s, report_id, "cooperative", NOW)
        # 事后发布的 v2 会把 4.0 修正成 9.5（超温），但不得影响复算。
        sc.s.add_curve(
            "INS-COOP", None, "polynomial",
            '{"coeffs": [5.5, 1.0], "uncertainty_c": 0.1}',
            "2026-09-15T00:00:00+08:00", "LAB-YN-01",
            "2026-09-15T00:00:00+08:00")
        rec = logic.recompute_dispute(
            sc.s, "YX-20260914-ROSE-01", report_id)
        self.assertTrue(rec["recompute_matches"])
        self.assertEqual(rec["verdict_snapshot"], "compliant")
        self.assertEqual(rec["verdict_recomputed"], "compliant")

    def test_out_of_range_curve_blocks(self):
        sc = self.sc
        sc.s.add_curve(
            "INS-COOP", None, "piecewise_linear",
            '{"points": [[5, 5], [10, 10]], "uncertainty_c": 0.1}',
            "2026-09-01T00:00:00+08:00", "LAB-YN-01", NOW)
        r = logic.evaluate(sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        self.assertIn("curve_out_of_range", r["summary"]["blockers"])
        self.assertEqual(r["verdict"], "inconclusive")


class FreezeAndRecomputeTest(unittest.TestCase):
    def setUp(self):
        self.sc = Scenario()
        sc = self.sc
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        sc.feed("INS-CAR", 0, 120, lambda m: 4.7)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)

    def test_late_backfill_after_signing_does_not_change_report(self):
        sc = self.sc
        report_id, _, snap = logic.create_report(
            sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        logic.sign_report(sc.s, report_id, "cooperative", NOW)
        adopted_before = len(snap["corrected"])
        # 签署之后补传整段超温数据。
        sc.feed("INS-CAR", 0, 120, lambda m: 12.0, backfill=True,
                ingestion="late")
        rec = logic.recompute_dispute(
            sc.s, "YX-20260914-ROSE-01", report_id)
        self.assertTrue(rec["snapshot_digest_intact"])
        self.assertTrue(rec["recompute_matches"])
        self.assertEqual(len(rec["adopted_readings"]), adopted_before)
        self.assertEqual(rec["verdict_snapshot"], "compliant")

    def test_recompute_lists_exclusions_bands_and_confirmations(self):
        sc = self.sc
        report_id, _, _ = logic.create_report(
            sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        logic.sign_report(sc.s, report_id, "cooperative", NOW)
        logic.sign_report(sc.s, report_id, "carrier", NOW)
        sc.s.add_confirmation(report_id, "COOP-YX", "accepted", None, NOW)
        sc.s.add_confirmation(report_id, "CARRIER-YX-01", "disputed",
                              "侧壁探头受日晒", NOW)
        rec = logic.recompute_dispute(
            sc.s, "YX-20260914-ROSE-01", report_id)
        self.assertEqual({s["signed_by"] for s in rec["signatures"]},
                         {"cooperative", "carrier"})
        self.assertEqual({c["decision"] for c in rec["confirmations"]},
                         {"accepted", "disputed"})
        self.assertTrue(rec["uncertainty_bands"])
        band = rec["uncertainty_bands"][0]
        self.assertLess(band["interval_c"][0], band["corrected_c"])
        self.assertGreater(band["interval_c"][1], band["corrected_c"])

    def test_tampered_snapshot_is_detected(self):
        import json
        sc = self.sc
        report_id, _, _ = logic.create_report(
            sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        # 直接改库会被追加只存触发器拦截；这里验证触发器确实存在。
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            sc.s.conn.execute(
                "UPDATE reports SET snapshot_json='{}' WHERE report_id=?",
                (report_id,))

    def test_confirmations_are_parties(self):
        sc = self.sc
        report_id, _, _ = logic.create_report(
            sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        sc.s.add_confirmation(report_id, "COOP-YX", "accepted", None, NOW)
        # 同一方不能重复确认。
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            sc.s.add_confirmation(report_id, "COOP-YX", "disputed", None, NOW)


class CoverageAndPlansTest(unittest.TestCase):
    def _lot(self, lot_id, route, packaging, season, verdict, coverage):
        s = self.s
        s.add_lot(lot_id, route_id=route, packaging=packaging, season=season,
                  now=NOW)
        s.add_assessment(
            lot_id, None, AS_OF, ts(0), ts(120), verdict,
            corrected=[], exclusions=[],
            inputs={"algorithm_version": logic.ALGORITHM_VERSION},
            summary={"effective_coverage": coverage,
                     "per_instrument": {}, "verdict": verdict},
            now=NOW)

    def setUp(self):
        self.s = Store()

    def test_compare_coverage_by_route_packaging_season(self):
        self._lot("L1", "R1", "foam", "summer", "compliant", 0.99)
        self._lot("L2", "R1", "paper", "summer", "inconclusive", 0.80)
        self._lot("L3", "R2", "foam", "winter", "exceeded", 0.96)
        by_route = {g["group"]: g for g in
                    logic.compare_coverage(self.s, "route_id")}
        self.assertLess(by_route["R1"]["avg_coverage"],
                        by_route["R2"]["avg_coverage"])
        by_pkg = {g["group"]: g for g in
                  logic.compare_coverage(self.s, "packaging")}
        self.assertEqual(by_pkg["paper"]["avg_coverage"], 0.80)
        by_season = {g["group"]: g for g in
                     logic.compare_coverage(self.s, "season")}
        self.assertEqual(set(by_season), {"summer", "winter"})

    def test_plans_for_expired_cert_and_fast_drift(self):
        s = self.s
        s.add_instrument("M1", "recorder", now=NOW, instrument_id="I1")
        s.add_calibration("I1", "LAB", "2025-01-01T00:00:00+08:00",
                          "2026-08-01T00:00:00+08:00", 0.3, NOW)
        s.add_clock_drift("I1", "2026-09-14T05:30:00+08:00",
                          "2026-09-14T05:30:00+08:00", 35.0, NOW)
        created = logic.generate_plans(s, NOW)
        kinds = {s.get_plan(p)["kind"] for p, _ in created}
        self.assertIn("recalibration", kinds)
        self.assertIn("rotation", kinds)

    def test_plans_for_never_calibrated(self):
        s = self.s
        s.add_instrument("M2", "probe", now=NOW, instrument_id="I2")
        logic.generate_plans(s, NOW)
        plans = s.list_plans()
        self.assertTrue(any(p["kind"] == "recalibration" for p in plans))

    def test_plan_generation_is_idempotent(self):
        s = self.s
        s.add_instrument("M3", "probe", now=NOW, instrument_id="I3")
        first = logic.generate_plans(s, NOW)
        second = logic.generate_plans(s, NOW)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [(first[0][0], False)])


class RedactionTest(unittest.TestCase):
    def test_evidence_package_is_redacted(self):
        sc = Scenario()
        sc.feed("INS-COOP", 0, 120, lambda m: 4.5)
        sc.feed("INS-CAR", 0, 120, lambda m: 4.7)
        sc.feed("INS-COLD", 120, 300, lambda m: 3.8)
        report_id, _, _ = logic.create_report(
            sc.s, "YX-20260914-ROSE-01", AS_OF, NOW)
        logic.sign_report(sc.s, report_id, "CARRIER-YX-01", NOW)
        package_id, pkg = logic.build_evidence_package(
            sc.s, "YX-20260914-ROSE-01", report_id, NOW)
        raw = str(pkg)
        for secret in ("INS-COOP", "INS-CAR", "INS-COLD", "CARRIER-YX-01",
                       "LAB-YN-01", "CoopLogger"):
            self.assertNotIn(secret, raw)
        probes = {r["probe"] for r in pkg["readings"]}
        self.assertTrue(probes and all(p.startswith("探头") for p in probes))
        self.assertEqual(pkg["verdict"], "compliant")
        # 证据包落库且可取回。
        self.assertEqual(sc.s.get_package(package_id)["redacted_json"], pkg)


if __name__ == "__main__":
    unittest.main()
