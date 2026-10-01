"""修正曲线：实验人员用标准温箱发布，按版本管理。

报告在生成时锁定所采用的曲线版本；之后发布的新版本
不会回溯改变已生成的报告，只能通过修正流程产生新报告版本。
"""

from __future__ import annotations

from .model import CorrectionCurve, iso, to_dict, utcnow
from .store import AppendOnlyStore


class PermissionDenied(Exception):
    """角色无权执行该操作。"""


def publish_curve(
    store: AppendOnlyStore,
    *,
    actor_role: str,
    device_model: str,
    points: list,
    published_by: str,
    residual_uncertainty_c: float,
    published_at: str | None = None,
) -> dict:
    """发布某设备型号的修正曲线新版本。仅实验人员可发布。"""
    if actor_role != "lab":
        raise PermissionDenied("只有实验人员可以发布修正曲线")
    pts = sorted(([float(r), float(c)] for r, c in points), key=lambda p: p[0])
    existing = [c for c in store.payloads("curve") if c["device_model"] == device_model]
    version = max((c["version"] for c in existing), default=0) + 1
    curve = CorrectionCurve(
        curve_id=f"CURVE-{device_model}-v{version}",
        device_model=device_model,
        version=version,
        points=pts,
        published_at=published_at or iso(utcnow()),
        published_by=published_by,
        residual_uncertainty_c=float(residual_uncertainty_c),
        supersedes=version - 1 if version > 1 else None,
    )
    return store.append("curve", to_dict(curve))["payload"]


def get_curve(store: AppendOnlyStore, device_model: str, version: int) -> dict | None:
    for c in store.payloads("curve"):
        if c["device_model"] == device_model and c["version"] == version:
            return c
    return None


def latest_curve(store: AppendOnlyStore, device_model: str) -> dict | None:
    curves = [c for c in store.payloads("curve") if c["device_model"] == device_model]
    if not curves:
        return None
    return max(curves, key=lambda c: c["version"])


def apply_curve(curve: dict, raw_c: float) -> float:
    """按曲线点对线性插值，返回修正后的温度。"""
    pts = curve["points"]
    if not pts:
        return raw_c
    if raw_c <= pts[0][0]:
        return raw_c + pts[0][1]
    if raw_c >= pts[-1][0]:
        return raw_c + pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= raw_c <= x1:
            ratio = (raw_c - x0) / (x1 - x0)
            return raw_c + y0 + ratio * (y1 - y0)
    return raw_c
