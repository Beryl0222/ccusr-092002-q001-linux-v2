"""存储层测试：追加只存、哈希链、幂等补传。"""

import sqlite3
import unittest

from coldchain.store import APPEND_ONLY_TABLES, Store

NOW = "2026-09-14T10:00:00+08:00"


class AppendOnlyTest(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        self.s.add_instrument("M1", "recorder", now=NOW, instrument_id="I1")
        self.s.add_lot("L1", now=NOW)

    def test_append_only_tables_reject_update_and_delete(self):
        s = self.s
        # readings
        rid, _, _ = s.append_reading(
            "I1", "L1", NOW, NOW, 4.0, ingestion_id="g1")
        # correction_curves
        s.add_curve("I1", None, "polynomial", '{"coeffs": [0, 1]}',
                    NOW, "lab", NOW, curve_id="CV1")
        # assessments -> reports -> signatures / confirmations -> package
        aid = s.add_assessment(
            "L1", None, NOW, NOW, NOW, "compliant", [], [], {}, {}, NOW,
            assessment_id="AS1")
        report_id = s.add_report(aid, "L1", None, "{}", NOW,
                                 report_id="RP1")
        s.add_signature(report_id, "cooperative", NOW)
        s.add_confirmation(report_id, "cooperative", "accepted", None, NOW)
        s.add_package("L1", report_id, {}, NOW, package_id="PK1")

        sample_values = {
            "readings": ("raw_c", "9.9"),
            "assessments": ("verdict", "'x'"),
            "reports": ("snapshot_json", "'x'"),
            "report_signatures": ("signed_by", "'x'"),
            "confirmations": ("comment", "'x'"),
            "correction_curves": ("note", "'x'"),
            "evidence_packages": ("redacted_json", "'x'"),
        }
        for table, (column, value) in sample_values.items():
            with self.subTest(table=table, op="update"):
                with self.assertRaises(sqlite3.IntegrityError):
                    s.conn.execute(
                        f"UPDATE {table} SET {column}={value}")
            with self.subTest(table=table, op="delete"):
                with self.assertRaises(sqlite3.IntegrityError):
                    s.conn.execute(f"DELETE FROM {table}")

    def test_reading_hash_chain_detects_rewrite(self):
        for i in range(5):
            self.s.append_reading(
                "I1", "L1", f"2026-09-14T10:{i:02d}:00+08:00",
                f"2026-09-14T10:{i:02d}:00+08:00", 4.0 + i,
                ingestion_id="g1")
        ok, broken = self.s.verify_chain("I1")
        self.assertTrue(ok)
        # 绕过触发器无法用常规 SQL 做到；直接破坏链指针验证核验有效性。
        self.s.conn.execute(
            "DROP TRIGGER readings_no_update")
        self.s.conn.commit()
        self.s.conn.execute(
            "UPDATE readings SET raw_c=9.9 WHERE rowid="
            "(SELECT rowid FROM readings ORDER BY rowid LIMIT 1 OFFSET 2)")
        self.s.conn.commit()
        ok, broken = self.s.verify_chain("I1")
        self.assertFalse(ok)
        self.assertIsNotNone(broken)

    def test_idempotent_ingestion_dedup(self):
        args = ("I1", "L1", NOW, NOW, 4.0)
        r1, stored1, dup1 = self.s.append_reading(*args, ingestion_id="batch-1")
        r2, stored2, dup2 = self.s.append_reading(*args, ingestion_id="batch-1")
        self.assertEqual(r1, r2)
        self.assertTrue(stored1)
        self.assertTrue(dup2)
        rows = self.s.readings_for_lot("L1", "9999-12-31T00:00:00+08:00")
        self.assertEqual(len(rows), 1)

    def test_curve_versions_monotonic(self):
        cid1, v1 = self.s.add_curve(
            "I1", None, "polynomial", '{"coeffs": [0, 1]}',
            NOW, "lab", NOW)
        cid2, v2 = self.s.add_curve(
            "I1", None, "polynomial", '{"coeffs": [1, 1]}',
            NOW, "lab", NOW)
        self.assertEqual((v1, v2), (1, 2))
        curve = self.s.curve_for("I1", "M1", NOW)
        self.assertEqual(curve["curve_id"], cid2)
        # 型号级曲线独立编号。
        _cid, mv = self.s.add_curve(
            None, "OtherModel", "polynomial", '{"coeffs": [0]}',
            NOW, "lab", NOW)
        self.assertEqual(mv, 1)

    def test_calibration_validity_window(self):
        self.s.add_calibration(
            "I1", "LAB", "2026-01-01T00:00:00+08:00",
            "2026-06-01T00:00:00+08:00", 0.3, NOW, cert_id="OLD")
        self.s.add_calibration(
            "I1", "LAB", "2026-06-01T00:00:00+08:00",
            "2027-06-01T00:00:00+08:00", 0.2, NOW, cert_id="NEW")
        self.assertEqual(
            self.s.cert_valid_at("I1", "2026-05-01T00:00:00+08:00")["cert_id"],
            "OLD")
        self.assertEqual(
            self.s.cert_valid_at("I1", "2026-09-14T10:00:00+08:00")["cert_id"],
            "NEW")
        self.assertIsNone(
            self.s.cert_valid_at("I1", "2025-12-31T00:00:00+08:00"))
        self.assertIsNone(
            self.s.cert_valid_at("I1", "2027-07-01T00:00:00+08:00"))

    def test_backfill_flag_and_dual_timestamps(self):
        self.s.append_reading(
            "I1", "L1", "2026-09-14T08:00:00+08:00",
            "2026-09-15T09:00:00+08:00", 5.1, backfill=True,
            ingestion_id="late")
        row = self.s.readings_for_lot("L1", "9999-12-31T00:00:00+08:00")[0]
        self.assertTrue(row["backfill"])
        self.assertNotEqual(row["device_time"], row["received_at"])


if __name__ == "__main__":
    unittest.main()
