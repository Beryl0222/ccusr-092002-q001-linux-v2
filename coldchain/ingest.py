"""读数接入：原始读数只追加不覆盖，补传数据双时间戳分别保存。"""

from __future__ import annotations

from datetime import timedelta

from .model import Reading, iso, parse_ts, to_dict, utcnow
from .store import AppendOnlyStore

# 设备时间与接收时间相差超过该阈值即视为补传
LATE_THRESHOLD = timedelta(minutes=15)


def ingest_reading(
    store: AppendOnlyStore,
    *,
    device_id: str,
    lot_id: str,
    segment_id: str,
    device_time: str,
    temperature_c: float,
    received_time: str | None = None,
) -> dict:
    """追加一条读数。

    同一设备同一设备时间的重复上报不会覆盖旧记录，而是以更大的
    sequence 追加；生效视图取 sequence 最大者，原始记录全部保留。
    """
    received = received_time or iso(utcnow())
    late = parse_ts(received) - parse_ts(device_time) > LATE_THRESHOLD
    sequence = sum(
        1
        for r in store.payloads("reading")
        if r["device_id"] == device_id and r["device_time"] == device_time
    )
    reading = Reading(
        device_id=device_id,
        lot_id=lot_id,
        segment_id=segment_id,
        device_time=device_time,
        received_time=received,
        temperature_c=temperature_c,
        sequence=sequence,
        late=late,
        reading_id=f"{device_id}@{device_time}#{sequence}",
    )
    return store.append("reading", to_dict(reading))


def raw_readings(store: AppendOnlyStore, lot_id: str, segment_id: str | None = None) -> list[dict]:
    """全部原始读数（含被后续补传"修正"的旧版本），按追加顺序。"""
    out = []
    for r in store.payloads("reading"):
        if r["lot_id"] != lot_id:
            continue
        if segment_id is not None and r["segment_id"] != segment_id:
            continue
        out.append(r)
    return out


def effective_readings(store: AppendOnlyStore, lot_id: str, segment_id: str | None = None) -> list[dict]:
    """生效视图：同一设备同一设备时间只保留 sequence 最大的记录。"""
    latest: dict[tuple[str, str], dict] = {}
    for r in raw_readings(store, lot_id, segment_id):
        key = (r["device_id"], r["device_time"])
        if key not in latest or r["sequence"] > latest[key]["sequence"]:
            latest[key] = r
    return sorted(latest.values(), key=lambda r: (r["device_time"], r["device_id"]))
