"""履约评估：质量门、修正曲线应用、不确定区间与可复算的报告摘要。

核心规则：
- 校准过期、采样缺口、多探头偏差过大或覆盖率不足时，不得直接形成
  履约结论，结论只能是"无法判定"（indeterminate）；
- 报告锁定所采用的修正曲线版本、阈值参数与时钟漂移，保证日后可复算；
- 摘要不包含生成时间与确认信息，保证复算结果可与签署报告比对。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, asdict
from datetime import timedelta

from .corrections import apply_curve, get_curve, latest_curve
from .ingest import effective_readings
from .model import iso, parse_ts, utcnow
from .registry import (
    binding_for,
    calibration_at,
    drift_at,
    get_device,
    get_lot,
)
from .store import AppendOnlyStore


@dataclass
class AssessmentParams:
    """评估阈值，随报告一同固化。"""

    max_gap_minutes: float = 10.0
    min_coverage: float = 0.8
    max_probe_deviation_c: float = 2.0
    align_tolerance_seconds: float = 120.0
    k: float = 2.0  # 扩展不确定度包含因子


def _corrected_time(store: AppendOnlyStore, reading: dict):
    """设备时间按时钟漂移修正后的标准时间。"""
    offset = drift_at(store, reading["device_id"], reading["device_time"])
    return parse_ts(reading["device_time"]) - timedelta(seconds=offset)


def classify_readings(
    store: AppendOnlyStore,
    lot_id: str,
    segment_id: str,
    window_start: str,
    window_end: str,
) -> tuple[list[dict], list[dict]]:
    """把窗口内的生效读数分为有效与排除两类。

    返回 (valid, exclusions)；exclusions 每项含 reading_id 与 reason。
    有效读数附带修正后温度 corrected_c 与合成不确定度 uncertainty_c。
    """
    ws, we = parse_ts(window_start), parse_ts(window_end)
    valid, exclusions = [], []
    for r in effective_readings(store, lot_id, segment_id):
        t = _corrected_time(store, r)
        if not (ws <= t <= we):
            continue
        device = get_device(store, r["device_id"])
        if device is None:
            exclusions.append({"reading_id": r["reading_id"], "reason": "设备未登记"})
            continue
        binding = binding_for(store, r["device_id"], lot_id, segment_id)
        if binding is None:
            exclusions.append({"reading_id": r["reading_id"], "reason": "设备未绑定该批次区段"})
            continue
        if not (parse_ts(binding["started_at"]) <= t <= parse_ts(binding["stopped_at"])):
            exclusions.append({"reading_id": r["reading_id"], "reason": "超出设备启停区间"})
            continue
        cert = calibration_at(store, r["device_id"], r["device_time"])
        if cert is None:
            has_any = any(
                c["device_id"] == r["device_id"] for c in store.payloads("calibration")
            )
            reason = "校准证书过期" if has_any else "缺少校准证书"
            exclusions.append({"reading_id": r["reading_id"], "reason": reason})
            continue
        valid.append({**r, "corrected_time": iso(t), "_cert": cert, "_device": device})
    return valid, exclusions


def _apply_corrections(store: AppendOnlyStore, valid: list[dict], curve_versions: dict | None):
    """为有效读数应用修正曲线并合成不确定度；返回采用的曲线版本。"""
    used_versions: dict[str, int] = {}
    for r in valid:
        model = r["_device"]["model"]
        if curve_versions is not None:
            # 复算场景：严格按报告锁定的版本取曲线；锁定为空表示当时无曲线
            curve = get_curve(store, model, curve_versions[model]) if model in curve_versions else None
        else:
            curve = latest_curve(store, model)
        residual = 0.0
        corrected = r["temperature_c"]
        if curve is not None:
            corrected = apply_curve(curve, r["temperature_c"])
            residual = curve["residual_uncertainty_c"]
            used_versions[model] = curve["version"]
        u = math.sqrt(
            r["_device"]["tolerance_c"] ** 2
            + r["_cert"]["uncertainty_c"] ** 2
            + residual ** 2
        )
        r["corrected_c"] = round(corrected, 4)
        r["standard_uncertainty_c"] = round(u, 4)
    return used_versions


def _coverage_and_gaps(valid: list[dict], ws, we, max_gap: timedelta):
    """返回 (覆盖率, 最大缺口分钟数)。读数覆盖按相邻间隔封顶 max_gap 估算。"""
    times = sorted(parse_ts(r["corrected_time"]) for r in valid)
    window_minutes = (we - ws).total_seconds() / 60.0
    if window_minutes <= 0:
        return 0.0, 0.0
    edges = [ws, *times, we]
    gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(edges, edges[1:])]
    cap = max_gap.total_seconds() / 60.0
    covered = sum(min(g, cap) for g in gaps)
    return covered / window_minutes, (max(gaps) if gaps else window_minutes)


def _probe_deviation(valid: list[dict], align_tolerance: timedelta) -> float | None:
    """多探头两两对齐读数的中位绝对偏差；不足两设备或无对齐对时返回 None。"""
    by_device: dict[str, list] = {}
    for r in valid:
        by_device.setdefault(r["device_id"], []).append(r)
    if len(by_device) < 2:
        return None
    diffs = []
    devices = sorted(by_device)
    for i, da in enumerate(devices):
        for db in devices[i + 1 :]:
            for ra in by_device[da]:
                ta = parse_ts(ra["corrected_time"])
                best = min(
                    by_device[db],
                    key=lambda rb: abs((parse_ts(rb["corrected_time"]) - ta).total_seconds()),
                )
                if abs((parse_ts(best["corrected_time"]) - ta).total_seconds()) <= align_tolerance.total_seconds():
                    diffs.append(abs(ra["corrected_c"] - best["corrected_c"]))
    if not diffs:
        return None
    diffs.sort()
    mid = len(diffs) // 2
    return diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) / 2


def digest_of(report: dict) -> str:
    """报告内容摘要：排除生成时间、确认信息与摘要本身，保证可复算。"""
    excluded = {"digest", "created_at", "confirmations", "signed"}
    content = {k: v for k, v in report.items() if k not in excluded}
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assess_lot(
    store: AppendOnlyStore,
    lot_id: str,
    segment_id: str,
    window_start: str,
    window_end: str,
    *,
    params: AssessmentParams | None = None,
    curve_versions: dict | None = None,
    report_version: int = 1,
    supersedes: str | None = None,
    amendment_reason: str | None = None,
    report_id: str | None = None,
) -> dict:
    """评估一个批次在某区段的温控履约情况，返回未签署的报告。"""
    params = params or AssessmentParams()
    lot = get_lot(store, lot_id)
    ws, we = parse_ts(window_start), parse_ts(window_end)
    valid, exclusions = classify_readings(store, lot_id, segment_id, window_start, window_end)
    used_curve_versions = _apply_corrections(store, valid, curve_versions)

    max_gap = timedelta(minutes=params.max_gap_minutes)
    coverage, worst_gap = _coverage_and_gaps(valid, ws, we, max_gap)
    deviation = _probe_deviation(valid, timedelta(seconds=params.align_tolerance_seconds))

    gates = [
        {
            "gate": "采样覆盖率",
            "passed": coverage >= params.min_coverage,
            "detail": f"覆盖率 {coverage:.1%}，要求 ≥{params.min_coverage:.0%}",
        },
        {
            "gate": "采样缺口",
            "passed": worst_gap <= params.max_gap_minutes,
            "detail": f"最大缺口 {worst_gap:.1f} 分钟，允许 ≤{params.max_gap_minutes:.0f} 分钟",
        },
        {
            "gate": "探头间偏差",
            "passed": deviation is None or deviation <= params.max_probe_deviation_c,
            "detail": (
                f"中位偏差 {deviation:.2f}℃" if deviation is not None else "单探头或无对齐样本"
            ),
        },
    ]
    gates_ok = all(g["passed"] for g in gates) and bool(valid)

    verdict = "indeterminate"
    peak = trough = interval_lo = interval_hi = None
    uncertainty = None
    if valid:
        k = params.k
        uppers = [r["corrected_c"] + k * r["standard_uncertainty_c"] for r in valid]
        lowers = [r["corrected_c"] - k * r["standard_uncertainty_c"] for r in valid]
        peak = max(r["corrected_c"] for r in valid)
        trough = min(r["corrected_c"] for r in valid)
        interval_lo, interval_hi = round(min(lowers), 2), round(max(uppers), 2)
        uncertainty = round(max(r["standard_uncertainty_c"] for r in valid) * k, 2)
        if gates_ok:
            hi_ok = max(uppers) <= lot["temp_max_c"]
            lo_ok = min(lowers) >= lot["temp_min_c"]
            hi_fail = min(lowers) > lot["temp_max_c"]
            lo_fail = max(uppers) < lot["temp_min_c"]
            if hi_ok and lo_ok:
                verdict = "compliant"
            elif hi_fail or lo_fail:
                verdict = "non_compliant"
            else:
                verdict = "indeterminate"  # 不确定区间跨限，不得直接下结论

    seq = sum(
        1
        for p in store.payloads("report")
        if p["lot_id"] == lot_id and p["segment_id"] == segment_id
    )
    report = {
        "report_id": report_id or f"RPT-{lot_id}-{segment_id}-{seq + 1}",
        "lot_id": lot_id,
        "segment_id": segment_id,
        "window": {"start": window_start, "end": window_end},
        "params": asdict(params),
        "curve_versions": used_curve_versions,
        "drift_seconds": {
            d: drift_at(store, d, window_start)
            for d in sorted({r["device_id"] for r in valid})
        },
        "used_reading_ids": sorted(r["reading_id"] for r in valid),
        "exclusions": sorted(exclusions, key=lambda e: e["reading_id"]),
        "gates": gates,
        "coverage": round(coverage, 4),
        "verdict": verdict,
        "peak_temperature_c": peak,
        "trough_temperature_c": trough,
        "uncertainty_c": uncertainty,
        "interval_c": [interval_lo, interval_hi] if interval_lo is not None else None,
        "version": report_version,
        "supersedes": supersedes,
        "amendment_reason": amendment_reason,
        "created_at": iso(utcnow()),
    }
    report["digest"] = digest_of(report)
    return report
