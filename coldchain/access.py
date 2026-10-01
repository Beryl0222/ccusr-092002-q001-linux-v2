"""角色视图：承运商区段隔离与理赔脱敏证据包。"""

from __future__ import annotations

import hashlib

from .ingest import effective_readings
from .registry import bindings_for_lot, get_device
from .reports import get_report, report_status
from .store import AppendOnlyStore


def _mask(value: str) -> str:
    """稳定脱敏：同一输入得到同一占位符，便于核对但不泄露原值。"""
    return "脱敏#" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def carrier_view(store: AppendOnlyStore, carrier_id: str, lot_id: str) -> dict:
    """承运商只能看到自己负责的区段及其读数。"""
    own = [b for b in bindings_for_lot(store, lot_id) if b["carrier_id"] == carrier_id]
    segments = []
    for binding in own:
        readings = effective_readings(store, lot_id, binding["segment_id"])
        segments.append(
            {
                "segment_id": binding["segment_id"],
                "window": {"start": binding["started_at"], "end": binding["stopped_at"]},
                "devices": sorted({r["device_id"] for r in readings}),
                "readings": readings,
            }
        )
    return {"lot_id": lot_id, "carrier_id": carrier_id, "segments": segments}


def claims_evidence_package(store: AppendOnlyStore, report_id: str) -> dict:
    """理赔证据包：以签署报告为准，设备所有人与序列号脱敏。

    包含当时采用的读数、排除理由、不确定区间与各方确认结果，
    不含任何可识别合作社、承运商或设备序列的信息。
    """
    report = get_report(store, report_id)
    status = report_status(store, report_id)
    devices: dict[str, dict] = {}
    for reading_id in report["used_reading_ids"]:
        device_id = reading_id.split("@", 1)[0]
        if device_id in devices:
            continue
        device = get_device(store, device_id) or {}
        devices[device_id] = {
            "device_ref": _mask(device_id),
            "model": device.get("model"),
            "install_position": device.get("install_position"),
        }
    used = []
    for r in effective_readings(store, report["lot_id"], report["segment_id"]):
        if r["reading_id"] in report["used_reading_ids"]:
            used.append(
                {
                    "device_ref": _mask(r["device_id"]),
                    "device_time": r["device_time"],
                    "received_time": r["received_time"],
                    "late": r["late"],
                    "temperature_c": r["temperature_c"],
                }
            )
    return {
        "report_id": report["report_id"],
        "digest": report["digest"],
        "lot_id": report["lot_id"],
        "segment_id": report["segment_id"],
        "window": report["window"],
        "verdict": report["verdict"],
        "interval_c": report["interval_c"],
        "uncertainty_c": report["uncertainty_c"],
        "gates": report["gates"],
        "exclusions": report["exclusions"],
        "devices": sorted(devices.values(), key=lambda d: d["device_ref"]),
        "readings": sorted(used, key=lambda r: r["device_time"]),
        "confirmations": status["confirmations"],
        "signed": status["signed"],
        "redacted": True,
    }
