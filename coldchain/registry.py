"""设备登记、校准证书、时钟漂移与批次绑定。"""

from __future__ import annotations

from .model import (
    Binding,
    CalibrationCertificate,
    ClockDrift,
    Device,
    Lot,
    parse_ts,
    to_dict,
)
from .store import AppendOnlyStore


def register_lot(store: AppendOnlyStore, lot: Lot) -> dict:
    return store.append("lot", to_dict(lot))


def register_device(store: AppendOnlyStore, device: Device) -> dict:
    return store.append("device", to_dict(device))


def register_calibration(store: AppendOnlyStore, cert: CalibrationCertificate) -> dict:
    return store.append("calibration", to_dict(cert))


def register_drift(store: AppendOnlyStore, drift: ClockDrift) -> dict:
    return store.append("drift", to_dict(drift))


def bind_device(store: AppendOnlyStore, binding: Binding) -> dict:
    return store.append("binding", to_dict(binding))


def get_lot(store: AppendOnlyStore, lot_id: str) -> dict:
    for payload in store.payloads("lot"):
        if payload["lot_id"] == lot_id:
            return payload
    raise KeyError(f"未登记的批次: {lot_id}")


def get_device(store: AppendOnlyStore, device_id: str) -> dict | None:
    for payload in store.payloads("device"):
        if payload["device_id"] == device_id:
            return payload
    return None


def calibration_at(store: AppendOnlyStore, device_id: str, moment: str) -> dict | None:
    """返回某一时刻有效的校准证书（多份重叠时取最近签发）。"""
    t = parse_ts(moment)
    best = None
    for cert in store.payloads("calibration"):
        if cert["device_id"] != device_id:
            continue
        if parse_ts(cert["valid_from"]) <= t <= parse_ts(cert["valid_until"]):
            if best is None or parse_ts(cert["issued_at"]) > parse_ts(best["issued_at"]):
                best = cert
    return best


def latest_calibration(store: AppendOnlyStore, device_id: str) -> dict | None:
    """返回最近签发的证书（不论是否已过期），用于轮换/复校计划。"""
    certs = [c for c in store.payloads("calibration") if c["device_id"] == device_id]
    if not certs:
        return None
    return max(certs, key=lambda c: parse_ts(c["issued_at"]))


def drift_at(store: AppendOnlyStore, device_id: str, moment: str) -> float:
    """返回某一时刻采用的时钟漂移（秒），取此前最近一次测定；无记录则为 0。"""
    t = parse_ts(moment)
    best = None
    for drift in store.payloads("drift"):
        if drift["device_id"] != device_id:
            continue
        if parse_ts(drift["measured_at"]) <= t:
            if best is None or parse_ts(drift["measured_at"]) > parse_ts(best["measured_at"]):
                best = drift
    return best["offset_seconds"] if best else 0.0


def binding_for(store: AppendOnlyStore, device_id: str, lot_id: str, segment_id: str) -> dict | None:
    for binding in store.payloads("binding"):
        if (
            binding["device_id"] == device_id
            and binding["lot_id"] == lot_id
            and binding["segment_id"] == segment_id
        ):
            return binding
    return None


def bindings_for_lot(store: AppendOnlyStore, lot_id: str) -> list[dict]:
    return [b for b in store.payloads("binding") if b["lot_id"] == lot_id]
