"""SQLite 持久化层。

设计原则：
- 原始读数（readings）只追加：触发器阻止 UPDATE/DELETE，同一设备的读数按入库顺序
  构成哈希链，任何篡改都会在 verify_chain 中暴露。
- 判定、报告、确认、签名、修正曲线、证据包同样只追加；“新版本”通过插入更高版本
  的行表达，绝不更新旧行。
- 设备档案（仪器、证书、区段、安装关系、令牌、计划）是治理元数据，允许正常维护。
"""

import hashlib
import sqlite3
import uuid

APPEND_ONLY_TABLES = (
    "readings",
    "assessments",
    "reports",
    "report_signatures",
    "confirmations",
    "correction_curves",
    "evidence_packages",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('recorder','probe','data_logger')),
    carrier_id TEXT,
    created_at TEXT NOT NULL,
    retired_at TEXT
);
CREATE TABLE IF NOT EXISTS calibration_certs (
    cert_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    lab TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    max_abs_error_c REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clock_drifts (
    drift_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    checked_at TEXT NOT NULL,
    device_clock_at TEXT NOT NULL,
    ppm REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY,
    cultivar TEXT,
    route_id TEXT,
    packaging TEXT,
    season TEXT,
    harvested_at TEXT,
    stems INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    segment_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    carrier_id TEXT NOT NULL,
    name TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    limit_min_c REAL,
    limit_max_c REAL,
    nominal_interval_s INTEGER NOT NULL DEFAULT 300,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS installations (
    install_id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL REFERENCES segments(segment_id),
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    position TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS correction_curves (
    curve_id TEXT PRIMARY KEY,
    instrument_id TEXT,
    instrument_model TEXT,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('polynomial','piecewise_linear')),
    params_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    note TEXT,
    CHECK(instrument_id IS NOT NULL OR instrument_model IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS readings (
    reading_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    segment_id TEXT REFERENCES segments(segment_id),
    device_time TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_c REAL NOT NULL,
    backfill INTEGER NOT NULL DEFAULT 0,
    ingestion_id TEXT NOT NULL,
    prev_hash TEXT,
    row_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    segment_id TEXT REFERENCES segments(segment_id),
    as_of TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    verdict TEXT NOT NULL,
    corrected_json TEXT NOT NULL,
    exclusions_json TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    segment_id TEXT REFERENCES segments(segment_id),
    assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_signatures (
    report_id TEXT NOT NULL REFERENCES reports(report_id),
    signed_by TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (report_id, signed_by)
);
CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(report_id),
    party TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accepted','disputed')),
    comment TEXT,
    confirmed_at TEXT NOT NULL,
    UNIQUE(report_id, party)
);
CREATE TABLE IF NOT EXISTS evidence_packages (
    package_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL,
    report_id TEXT REFERENCES reports(report_id),
    redacted_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('rotation','recalibration')),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('admin','lab','carrier','claims')),
    party_id TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_dedup
    ON readings(instrument_id, device_time, ingestion_id);
CREATE INDEX IF NOT EXISTS idx_readings_lot_time
    ON readings(lot_id, device_time);
CREATE INDEX IF NOT EXISTS idx_assessments_lot ON assessments(lot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_plans_open ON plans(kind, closed_at);
"""

# 追加只存触发器：任何更新或删除都被 SQLite 直接拒绝。
_IMMUTABLE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} 为追加只存表，禁止更新'); END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} 为追加只存表，禁止删除'); END;
"""


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Store:
    """封装所有 SQL，业务逻辑不直接拼语句。"""

    def __init__(self, path=":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_db()

    def init_db(self):
        cur = self.conn.executescript(SCHEMA)
        for table in APPEND_ONLY_TABLES:
            cur.executescript(_IMMUTABLE_TRIGGERS.format(table=table))
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- 治理元数据 -----------------------------------------------------

    def add_instrument(self, model, kind, carrier_id=None, now=None, instrument_id=None):
        instrument_id = instrument_id or new_id("INS")
        self.conn.execute(
            "INSERT INTO instruments VALUES (?,?,?,?,?,NULL)",
            (instrument_id, model, kind, carrier_id, now),
        )
        self.conn.commit()
        return instrument_id

    def get_instrument(self, instrument_id):
        row = self.conn.execute(
            "SELECT * FROM instruments WHERE instrument_id=?", (instrument_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_calibration(self, instrument_id, lab, valid_from, valid_until,
                        max_abs_error_c, now, cert_id=None):
        cert_id = cert_id or new_id("CERT")
        self.conn.execute(
            "INSERT INTO calibration_certs VALUES (?,?,?,?,?,?,?)",
            (cert_id, instrument_id, lab, valid_from, valid_until,
             max_abs_error_c, now),
        )
        self.conn.commit()
        return cert_id

    def cert_valid_at(self, instrument_id, at):
        """返回设备在指定时刻有效的校准证书，过期或缺失返回 None。"""
        row = self.conn.execute(
            """SELECT * FROM calibration_certs
               WHERE instrument_id=? AND valid_from<=? AND valid_until>=?
               ORDER BY valid_until DESC LIMIT 1""",
            (instrument_id, at, at),
        ).fetchone()
        return dict(row) if row else None

    def latest_cert(self, instrument_id):
        row = self.conn.execute(
            "SELECT * FROM calibration_certs WHERE instrument_id=? "
            "ORDER BY valid_until DESC LIMIT 1",
            (instrument_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_cert(self, cert_id):
        row = self.conn.execute(
            "SELECT * FROM calibration_certs WHERE cert_id=?", (cert_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_clock_drift(self, instrument_id, checked_at, device_clock_at, ppm, now,
                        drift_id=None):
        drift_id = drift_id or new_id("DRIFT")
        self.conn.execute(
            "INSERT INTO clock_drifts VALUES (?,?,?,?,?,?)",
            (drift_id, instrument_id, checked_at, device_clock_at, ppm, now),
        )
        self.conn.commit()
        return drift_id

    def latest_drift(self, instrument_id):
        row = self.conn.execute(
            "SELECT * FROM clock_drifts WHERE instrument_id=? "
            "ORDER BY checked_at DESC LIMIT 1",
            (instrument_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_drift(self, drift_id):
        row = self.conn.execute(
            "SELECT * FROM clock_drifts WHERE drift_id=?", (drift_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_lot(self, lot_id, cultivar=None, route_id=None, packaging=None,
                season=None, harvested_at=None, stems=None, now=None):
        self.conn.execute(
            "INSERT INTO lots VALUES (?,?,?,?,?,?,?,?)",
            (lot_id, cultivar, route_id, packaging, season, harvested_at, stems, now),
        )
        self.conn.commit()
        return lot_id

    def get_lot(self, lot_id):
        row = self.conn.execute("SELECT * FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
        return dict(row) if row else None

    def add_segment(self, lot_id, carrier_id, name, start_at, end_at,
                    limit_min_c=None, limit_max_c=None, nominal_interval_s=300,
                    now=None, segment_id=None):
        segment_id = segment_id or new_id("SEG")
        self.conn.execute(
            "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?)",
            (segment_id, lot_id, carrier_id, name, start_at, end_at,
             limit_min_c, limit_max_c, nominal_interval_s, now),
        )
        self.conn.commit()
        return segment_id

    def segments_for_lot(self, lot_id):
        rows = self.conn.execute(
            "SELECT * FROM segments WHERE lot_id=? ORDER BY start_at", (lot_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_segment(self, segment_id):
        row = self.conn.execute(
            "SELECT * FROM segments WHERE segment_id=?", (segment_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_installation(self, segment_id, instrument_id, position, start_at, end_at,
                         now=None, install_id=None):
        install_id = install_id or new_id("INST")
        self.conn.execute(
            "INSERT INTO installations VALUES (?,?,?,?,?,?,?)",
            (install_id, segment_id, instrument_id, position, start_at, end_at, now),
        )
        self.conn.commit()
        return install_id

    def installations_for_lot(self, lot_id):
        rows = self.conn.execute(
            """SELECT i.*, s.lot_id, s.carrier_id, s.nominal_interval_s,
                      s.limit_min_c, s.limit_max_c
               FROM installations i JOIN segments s ON i.segment_id = s.segment_id
               WHERE s.lot_id=? ORDER BY i.start_at""",
            (lot_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_token(self, token, role, party_id, label=None, now=None):
        self.conn.execute(
            "INSERT INTO tokens VALUES (?,?,?,?,?,NULL)",
            (token, role, party_id, label, now),
        )
        self.conn.commit()

    def get_token(self, token):
        row = self.conn.execute(
            "SELECT * FROM tokens WHERE token=? AND revoked_at IS NULL", (token,)
        ).fetchone()
        return dict(row) if row else None

    # ---- 修正曲线（追加只存，版本递增）----------------------------------

    def add_curve(self, instrument_id, instrument_model, kind, params_json,
                  valid_from, published_by, published_at, note=None, curve_id=None):
        if instrument_id is None and instrument_model is None:
            raise ValueError("修正曲线必须绑定设备或型号")
        if instrument_id is not None:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM correction_curves "
                "WHERE instrument_id=?",
                (instrument_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM correction_curves "
                "WHERE instrument_model=? AND instrument_id IS NULL",
                (instrument_model,),
            ).fetchone()
        version = row["v"] + 1
        curve_id = curve_id or new_id("CURVE")
        self.conn.execute(
            "INSERT INTO correction_curves VALUES (?,?,?,?,?,?,?,?,?,?)",
            (curve_id, instrument_id, instrument_model, version, kind,
             params_json, valid_from, published_by, published_at, note),
        )
        self.conn.commit()
        return curve_id, version

    def curve_for(self, instrument_id, instrument_model, at):
        """选择适用曲线：优先设备级，其次型号级；取 valid_from<=at 的最高版本。"""
        for sql, arg in (
            ("SELECT * FROM correction_curves WHERE instrument_id=? "
             "AND valid_from<=? ORDER BY version DESC LIMIT 1", (instrument_id, at)),
            ("SELECT * FROM correction_curves WHERE instrument_id IS NULL "
             "AND instrument_model=? AND valid_from<=? ORDER BY version DESC LIMIT 1",
             (instrument_model, at)),
        ):
            row = self.conn.execute(sql, arg).fetchone()
            if row:
                return dict(row)
        return None

    def get_curve(self, curve_id):
        row = self.conn.execute(
            "SELECT * FROM correction_curves WHERE curve_id=?", (curve_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- 原始读数（追加只存 + 哈希链）-----------------------------------

    @staticmethod
    def _hash_row(prev_hash, fields):
        material = "|".join(str(x) for x in fields)
        return hashlib.sha256(((prev_hash or "") + "|" + material).encode("utf-8")).hexdigest()

    def append_reading(self, instrument_id, lot_id, device_time, received_at, raw_c,
                       backfill=False, ingestion_id=None, segment_id=None,
                       reading_id=None):
        """追加一条原始读数；同一 ingestion 内重复（同设备同设备时间）幂等忽略。

        返回 (reading_id, stored, duplicate)：duplicate=True 表示该批次重复推送，
        数据库未改动。
        """
        reading_id = reading_id or new_id("RD")
        prev = self.conn.execute(
            "SELECT row_hash FROM readings WHERE instrument_id=? ORDER BY rowid DESC LIMIT 1",
            (instrument_id,),
        ).fetchone()
        prev_hash = prev["row_hash"] if prev else None
        row_hash = self._hash_row(prev_hash, (
            reading_id, instrument_id, lot_id, segment_id or "",
            device_time, received_at, raw_c, 1 if backfill else 0, ingestion_id,
        ))
        try:
            self.conn.execute(
                "INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (reading_id, instrument_id, lot_id, segment_id, device_time,
                 received_at, raw_c, 1 if backfill else 0, ingestion_id,
                 prev_hash, row_hash),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            existing = self.conn.execute(
                "SELECT reading_id FROM readings WHERE instrument_id=? "
                "AND device_time=? AND ingestion_id=?",
                (instrument_id, device_time, ingestion_id),
            ).fetchone()
            return existing["reading_id"], False, True
        self.conn.commit()
        return reading_id, True, False

    def readings_for_lot(self, lot_id, as_of):
        """取批次下接收时间不晚于 as_of 的全部读数（按设备时间排序）。"""
        rows = self.conn.execute(
            "SELECT * FROM readings WHERE lot_id=? AND received_at<=? "
            "ORDER BY device_time, received_at",
            (lot_id, as_of),
        ).fetchall()
        return [dict(r) for r in rows]

    def verify_chain(self, instrument_id):
        """重放某设备的哈希链，返回 (是否完整, 断裂处 reading_id 或 None)。"""
        rows = self.conn.execute(
            "SELECT * FROM readings WHERE instrument_id=? ORDER BY rowid",
            (instrument_id,),
        ).fetchall()
        prev_hash = None
        for r in rows:
            expect = self._hash_row(prev_hash, (
                r["reading_id"], r["instrument_id"], r["lot_id"],
                r["segment_id"] or "", r["device_time"], r["received_at"],
                r["raw_c"], r["backfill"], r["ingestion_id"],
            ))
            if r["prev_hash"] != prev_hash or r["row_hash"] != expect:
                return False, r["reading_id"]
            prev_hash = r["row_hash"]
        return True, None

    # ---- 判定 / 报告 / 证据包（追加只存）--------------------------------

    def add_assessment(self, lot_id, segment_id, as_of, window_start, window_end,
                       verdict, corrected, exclusions, inputs, summary, now,
                       assessment_id=None):
        import json
        assessment_id = assessment_id or new_id("ASM")
        self.conn.execute(
            "INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (assessment_id, lot_id, segment_id, as_of, window_start, window_end,
             verdict, json.dumps(corrected, ensure_ascii=False),
             json.dumps(exclusions, ensure_ascii=False),
             json.dumps(inputs, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), now),
        )
        self.conn.commit()
        return assessment_id

    def get_assessment(self, assessment_id):
        import json
        row = self.conn.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("corrected_json", "exclusions_json", "inputs_json", "summary_json"):
            d[key] = json.loads(d[key])
        return d

    def latest_assessment(self, lot_id, segment_id=None):
        if segment_id is None:
            row = self.conn.execute(
                "SELECT * FROM assessments WHERE lot_id=? AND segment_id IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (lot_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM assessments WHERE segment_id=? ORDER BY created_at DESC LIMIT 1",
                (segment_id,),
            ).fetchone()
        return self.get_assessment(row["assessment_id"]) if row else None

    def all_latest_assessments(self):
        """每个 (lot, segment) 范围取最新判定，用于覆盖率统计。"""
        rows = self.conn.execute(
            """SELECT a.* FROM assessments a JOIN (
                 SELECT lot_id, COALESCE(segment_id,'') seg, MAX(created_at) m
                 FROM assessments GROUP BY lot_id, COALESCE(segment_id,'')
               ) t ON a.lot_id=t.lot_id
                  AND COALESCE(a.segment_id,'')=t.seg AND a.created_at=t.m"""
        ).fetchall()
        import json
        out = []
        for row in rows:
            d = dict(row)
            for key in ("corrected_json", "exclusions_json", "inputs_json", "summary_json"):
                d[key] = json.loads(d[key])
            out.append(d)
        return out

    def add_report(self, assessment_id, lot_id, segment_id, snapshot_json, now,
                   report_id=None):
        report_id = report_id or new_id("RPT")
        self.conn.execute(
            "INSERT INTO reports VALUES (?,?,?,?,?,?)",
            (report_id, lot_id, segment_id, assessment_id, snapshot_json, now),
        )
        self.conn.commit()
        return report_id

    def get_report(self, report_id):
        row = self.conn.execute(
            "SELECT * FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_signature(self, report_id, signed_by, signed_at):
        self.conn.execute(
            "INSERT INTO report_signatures VALUES (?,?,?)",
            (report_id, signed_by, signed_at),
        )
        self.conn.commit()

    def signatures(self, report_id):
        rows = self.conn.execute(
            "SELECT * FROM report_signatures WHERE report_id=? ORDER BY signed_at",
            (report_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_confirmation(self, report_id, party, decision, comment, confirmed_at):
        confirmation_id = new_id("CNF")
        self.conn.execute(
            "INSERT INTO confirmations VALUES (?,?,?,?,?,?)",
            (confirmation_id, report_id, party, decision, comment, confirmed_at),
        )
        self.conn.commit()
        return confirmation_id

    def confirmations(self, report_id):
        rows = self.conn.execute(
            "SELECT * FROM confirmations WHERE report_id=? ORDER BY confirmed_at",
            (report_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_package(self, lot_id, report_id, redacted, now, package_id=None):
        import json
        package_id = package_id or new_id("PKG")
        self.conn.execute(
            "INSERT INTO evidence_packages VALUES (?,?,?,?,?)",
            (package_id, lot_id, report_id,
             json.dumps(redacted, ensure_ascii=False), now),
        )
        self.conn.commit()
        return package_id

    def get_package(self, package_id):
        import json
        row = self.conn.execute(
            "SELECT * FROM evidence_packages WHERE package_id=?", (package_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["redacted_json"] = json.loads(d["redacted_json"])
        return d

    # ---- 轮换 / 复校计划 -------------------------------------------------

    def add_plan(self, kind, target_type, target_id, due_at, reason, now):
        existing = self.conn.execute(
            "SELECT plan_id FROM plans WHERE kind=? AND target_type=? AND target_id=? "
            "AND closed_at IS NULL",
            (kind, target_type, target_id),
        ).fetchone()
        if existing:
            return existing["plan_id"], False
        plan_id = new_id("PLAN")
        self.conn.execute(
            "INSERT INTO plans VALUES (?,?,?,?,?,?,?,NULL)",
            (plan_id, kind, target_type, target_id, due_at, reason, now),
        )
        self.conn.commit()
        return plan_id, True

    def list_plans(self, include_closed=False):
        sql = "SELECT * FROM plans"
        if not include_closed:
            sql += " WHERE closed_at IS NULL"
        sql += " ORDER BY due_at"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def get_plan(self, plan_id):
        row = self.conn.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def all_instruments(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM instruments WHERE retired_at IS NULL").fetchall()]

    def all_lots(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM lots").fetchall()]
