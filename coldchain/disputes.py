"""争议复算：按签署报告固化的参数与曲线版本重放评估。

复算结果与签署摘要比对，可回答：当时采用了哪些读数、排除了哪些
及理由、不确定区间是多少、各方是否已确认，以及此后是否有补传
数据或新曲线悄然改变了结论。
"""

from __future__ import annotations

from .assessment import AssessmentParams, assess_lot
from .reports import get_report, report_status
from .store import AppendOnlyStore


def recompute_report(store: AppendOnlyStore, report_id: str) -> dict:
    """复算指定报告，返回比对结果。"""
    original = get_report(store, report_id)
    rebuilt = assess_lot(
        store,
        original["lot_id"],
        original["segment_id"],
        original["window"]["start"],
        original["window"]["end"],
        params=AssessmentParams(**original["params"]),
        curve_versions=original["curve_versions"],
        report_version=original["version"],
        supersedes=original["supersedes"],
        amendment_reason=original["amendment_reason"],
        report_id=original["report_id"],
    )
    differences = []
    for key in (
        "used_reading_ids",
        "exclusions",
        "gates",
        "verdict",
        "interval_c",
        "uncertainty_c",
        "coverage",
    ):
        if rebuilt[key] != original[key]:
            differences.append(
                {"field": key, "signed": original[key], "recomputed": rebuilt[key]}
            )
    status = report_status(store, report_id)
    return {
        "report_id": report_id,
        "signed_digest": original["digest"],
        "recomputed_digest": rebuilt["digest"],
        "reproducible": rebuilt["digest"] == original["digest"],
        "verdict": original["verdict"],
        "used_reading_ids": original["used_reading_ids"],
        "exclusions": original["exclusions"],
        "interval_c": original["interval_c"],
        "uncertainty_c": original["uncertainty_c"],
        "confirmations": status["confirmations"],
        "signed": status["signed"],
        "differences": differences,
    }
