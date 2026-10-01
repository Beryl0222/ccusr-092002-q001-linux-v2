"""报告签署：确认记录独立追加，签署后报告本体不可变。

任何后续修正（新读数、新曲线版本、阈值调整）都只能产生
新的报告版本并显式引用被替代的摘要，已签署报告保持原样。
"""

from __future__ import annotations

from .assessment import assess_lot, AssessmentParams
from .model import iso, utcnow
from .store import AppendOnlyStore

# 需要各方共同确认的角色
REQUIRED_PARTIES = ("association", "carrier", "consignor")


def save_report(store: AppendOnlyStore, report: dict) -> dict:
    return store.append("report", report)["payload"]


def get_report(store: AppendOnlyStore, report_id: str) -> dict:
    for p in store.payloads("report"):
        if p["report_id"] == report_id:
            return p
    raise KeyError(f"未找到报告: {report_id}")


def confirmations_for(store: AppendOnlyStore, report_id: str) -> list[dict]:
    return [c for c in store.payloads("confirmation") if c["report_id"] == report_id]


def confirm_report(store: AppendOnlyStore, report_id: str, party: str, confirmed_at: str | None = None) -> dict:
    """追加一方确认；确认是独立记录，不改动报告本体。"""
    report = get_report(store, report_id)
    if party not in REQUIRED_PARTIES:
        raise ValueError(f"未知确认方: {party}")
    if any(c["party"] == party for c in confirmations_for(store, report_id)):
        raise ValueError(f"{party} 已确认过报告 {report_id}")
    return store.append(
        "confirmation",
        {
            "report_id": report_id,
            "report_digest": report["digest"],
            "party": party,
            "confirmed_at": confirmed_at or iso(utcnow()),
        },
    )["payload"]


def report_status(store: AppendOnlyStore, report_id: str) -> dict:
    """报告当前状态：各方确认情况与是否已签署生效。"""
    report = get_report(store, report_id)
    confirmations = confirmations_for(store, report_id)
    confirmed = {c["party"] for c in confirmations}
    return {
        "report_id": report_id,
        "digest": report["digest"],
        "verdict": report["verdict"],
        "confirmations": confirmations,
        "pending_parties": [p for p in REQUIRED_PARTIES if p not in confirmed],
        "signed": all(p in confirmed for p in REQUIRED_PARTIES),
    }


def amend_report(
    store: AppendOnlyStore,
    report_id: str,
    *,
    reason: str,
    params: AssessmentParams | None = None,
) -> dict:
    """对已签署报告发起修正：重新评估并生成引用旧摘要的新版本。

    旧报告记录不被修改；新报告携带 supersedes 与修正原因。
    """
    status = report_status(store, report_id)
    if not status["signed"]:
        raise ValueError("报告尚未签署，无需修正流程，直接重新评估即可")
    old = get_report(store, report_id)
    new_report = assess_lot(
        store,
        old["lot_id"],
        old["segment_id"],
        old["window"]["start"],
        old["window"]["end"],
        params=params or AssessmentParams(**old["params"]),
        curve_versions=None,  # 修正采用当前最新曲线版本
        report_version=old["version"] + 1,
        supersedes=old["digest"],
        amendment_reason=reason,
    )
    return save_report(store, new_report)
