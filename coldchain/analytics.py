"""覆盖率分析与设备轮换/复校计划。"""

from __future__ import annotations

from datetime import timedelta

from .assessment import AssessmentParams, _coverage_and_gaps, classify_readings
from .model import parse_ts, season_of, utcnow
from .registry import bindings_for_lot, get_lot, latest_calibration
from .store import AppendOnlyStore


def coverage_report(store: AppendOnlyStore, params: AssessmentParams | None = None) -> list[dict]:
    """按路线、包装、季节比较有效数据覆盖率。

    对每个批次×区段绑定：有效读数（通过登记、绑定、启停区间与校准
    检查的读数）在启停区间内的覆盖比例，按维度聚合。
    """
    params = params or AssessmentParams()
    groups: dict[tuple, dict] = {}
    for lot_payload in store.payloads("lot"):
        lot = get_lot(store, lot_payload["lot_id"])
        for binding in bindings_for_lot(store, lot["lot_id"]):
            valid, _ = classify_readings(
                store,
                lot["lot_id"],
                binding["segment_id"],
                binding["started_at"],
                binding["stopped_at"],
            )
            coverage, _ = _coverage_and_gaps(
                valid,
                parse_ts(binding["started_at"]),
                parse_ts(binding["stopped_at"]),
                timedelta(minutes=params.max_gap_minutes),
            )
            key = (lot["route_id"], lot["packaging"], season_of(lot["harvested_at"]))
            bucket = groups.setdefault(
                key, {"segments": 0, "coverage_sum": 0.0, "lots": set()}
            )
            bucket["segments"] += 1
            bucket["coverage_sum"] += coverage
            bucket["lots"].add(lot["lot_id"])
    rows = []
    for (route, packaging, season), bucket in sorted(groups.items()):
        rows.append(
            {
                "route_id": route,
                "packaging": packaging,
                "season": season,
                "segments": bucket["segments"],
                "lots": len(bucket["lots"]),
                "avg_coverage": round(bucket["coverage_sum"] / bucket["segments"], 4),
            }
        )
    return rows


def maintenance_plan(
    store: AppendOnlyStore,
    *,
    as_of: str | None = None,
    horizon_days: int = 30,
    exclusion_rate_threshold: float = 0.2,
) -> list[dict]:
    """生成设备轮换与复校计划。

    - 校准证书在 horizon 内到期（或已过期）→ 复校任务；
    - 设备在已生成报告中的读数排除率超过阈值 → 轮换任务。
    """
    now = parse_ts(as_of) if as_of else utcnow()
    horizon = now + timedelta(days=horizon_days)

    excluded_counts: dict[str, int] = {}
    used_counts: dict[str, int] = {}
    for report in store.payloads("report"):
        for rid in report["used_reading_ids"]:
            device_id = rid.split("@", 1)[0]
            used_counts[device_id] = used_counts.get(device_id, 0) + 1
        for ex in report["exclusions"]:
            device_id = ex["reading_id"].split("@", 1)[0]
            excluded_counts[device_id] = excluded_counts.get(device_id, 0) + 1

    tasks = []
    for device in store.payloads("device"):
        device_id = device["device_id"]
        cert = latest_calibration(store, device_id)
        if cert is None:
            tasks.append(
                {"device_id": device_id, "type": "复校", "reason": "无校准证书", "due": None}
            )
        else:
            valid_until = parse_ts(cert["valid_until"])
            if valid_until <= horizon:
                tasks.append(
                    {
                        "device_id": device_id,
                        "type": "复校",
                        "reason": "校准即将到期" if valid_until > now else "校准已过期",
                        "due": cert["valid_until"],
                    }
                )
        total = used_counts.get(device_id, 0) + excluded_counts.get(device_id, 0)
        if total:
            rate = excluded_counts.get(device_id, 0) / total
            if rate > exclusion_rate_threshold:
                tasks.append(
                    {
                        "device_id": device_id,
                        "type": "轮换",
                        "reason": f"读数排除率 {rate:.0%} 超过阈值",
                        "due": None,
                    }
                )
    order = {"复校": 0, "轮换": 1}
    return sorted(tasks, key=lambda t: (order[t["type"]], t["due"] or "9999", t["device_id"]))
