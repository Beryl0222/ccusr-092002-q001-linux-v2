"""冷链测量治理的领域模型与时间工具。

所有时间一律使用带时区的 ISO 8601 字符串（如 2026-09-14T05:40:00+08:00）。
读数的设备时间与接收时间分别保存，补传数据不覆盖原始记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone


def parse_ts(value: str) -> datetime:
    """解析 ISO 8601 时间字符串，兼容尾部 Z。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区信息: {value}")
    return dt


def iso(dt: datetime) -> str:
    return dt.isoformat()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Lot:
    """采切批次，对应 contracts/harvest_lot.json 的扩展。"""

    lot_id: str
    cultivar: str
    harvested_at: str
    route_id: str
    packaging: str
    temp_min_c: float
    temp_max_c: float

    @property
    def season(self) -> str:
        month = parse_ts(self.harvested_at).month
        if month in (3, 4, 5):
            return "春"
        if month in (6, 7, 8):
            return "夏"
        if month in (9, 10, 11):
            return "秋"
        return "冬"


@dataclass
class Device:
    """测温设备：型号、所属方、安装位置与出厂允差。"""

    device_id: str
    model: str
    owner_id: str
    owner_name: str
    serial: str
    install_position: str
    tolerance_c: float = 0.5


@dataclass
class CalibrationCertificate:
    """校准证书，含有效期与校准不确定度。"""

    cert_id: str
    device_id: str
    lab: str
    issued_at: str
    valid_from: str
    valid_until: str
    uncertainty_c: float


@dataclass
class ClockDrift:
    """时钟漂移测定：offset_seconds = 设备时间 - 标准时间。"""

    device_id: str
    measured_at: str
    offset_seconds: float


@dataclass
class Binding:
    """设备与批次/路线区段的绑定，含启停区间。"""

    device_id: str
    lot_id: str
    segment_id: str
    carrier_id: str
    started_at: str
    stopped_at: str


@dataclass
class Reading:
    """一条原始温度读数。sequence 区分同一设备同一时刻的重复上报。"""

    device_id: str
    lot_id: str
    segment_id: str
    device_time: str
    received_time: str
    temperature_c: float
    sequence: int = 0
    late: bool = False
    reading_id: str = ""


@dataclass
class CorrectionCurve:
    """实验人员用标准温箱发布的修正曲线，按版本管理。"""

    curve_id: str
    device_model: str
    version: int
    points: list  # [(raw_c, correction_c)]，按 raw_c 升序
    published_at: str
    published_by: str
    residual_uncertainty_c: float
    supersedes: int | None = None


def to_dict(obj) -> dict:
    return asdict(obj)


def season_of(harvested_at: str) -> str:
    return Lot("", "", harvested_at, "", "", 0.0, 0.0).season
