"""冷链测量治理的核心领域逻辑（不依赖 HTTP，可独立复算）。

判定纪律：校准过期、时钟无核查、采样缺口导致覆盖率不足、多探头偏差无法被
不确定度解释时，结论只能是 inconclusive，不得直接给出履约合格 / 超温判定。
"""

import json
import math
from datetime import datetime, timedelta

ALGORITHM_VERSION = "cc-assess-1.0.0"
EXPANSION_FACTOR = 2.0  # 合成标准不确定度 -> 约 95% 扩展不确定度

DEFAULT_OPTS = {
    "min_coverage": 0.95,        # 有效数据覆盖率下限
    "gap_factor": 1.5,           # 相邻读数间隔超过 1.5 倍标称间隔记为缺口
    "divergence_threshold_c": 1.0,
    "recal_horizon_days": 30,
    "rotation_low_coverage": 0.90,
}

SEASONS = ("spring", "summer", "autumn", "winter")


# ---- 时间与数值辅助 -------------------------------------------------------

def parse_ts(value):
    """解析 ISO 8601；假定业务时间均带时区偏移。"""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间戳缺少时区信息: {value}")
    return dt


def iso(dt):
    return dt.isoformat()


def canonical_json(obj):
    """签署 / 复算比对使用的规范序列化。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def normalize_clock(device_time, drift):
    """依据最近一次时钟核查，把设备时间归一到真实时间。

    核查时刻设备时钟与真实时间的偏差为 offset_at_check；ppm 为设备相对走时率，
    正 ppm 表示时钟偏快。真实时间 = 设备时间 - (核查时偏差 + 走时漂移)。
    """
    device_dt = parse_ts(device_time)
    checked = parse_ts(drift["checked_at"])
    device_at_check = parse_ts(drift["device_clock_at"])
    offset_at_check = device_at_check - checked
    elapsed = (device_dt - device_at_check).total_seconds()
    drift_delta = timedelta(seconds=drift["ppm"] * 1e-6 * elapsed)
    residual_s = abs(drift["ppm"]) * 1e-6 * max(elapsed, 0.0)
    return device_dt - offset_at_check - drift_delta, residual_s


def eval_polynomial(coeffs, x):
    return sum(a * x ** i for i, a in enumerate(coeffs))


def eval_piecewise(points, x):
    pts = sorted((float(a), float(b)) for a, b in points)
    if len(pts) < 2:
        raise ValueError("分段线性曲线至少需要两个标定点")
    if x <= pts[0][0]:
        (x0, y0), (x1, y1) = pts[0], pts[1]
        return y0 + (y1 - y0) / (x1 - x0) * (x - x0), x < pts[0][0]
    if x >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        return y0 + (y1 - y0) / (x1 - x0) * (x - x0), x > pts[-1][0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) / (x1 - x0) * (x - x0), False
    return pts[-1][1], True


def apply_curve(curve, raw_c):
    """返回 (修正后温度, 曲线不确定度℃, 是否外推)。"""
    params = json.loads(curve["params_json"])
    out_of_range = False
    if curve["kind"] == "polynomial":
        corrected = eval_polynomial(params["coeffs"], raw_c)
    elif curve["kind"] == "piecewise_linear":
        corrected, out_of_range = eval_piecewise(params["points"], raw_c)
    else:
        raise ValueError(f"未知曲线类型: {curve['kind']}")
    return corrected, float(params.get("uncertainty_c", 0.0)), out_of_range


def _combine_uncertainty(cert_error, curve_uncertainty):
    return math.sqrt(float(cert_error) ** 2 + float(curve_uncertainty) ** 2)


# ---- 判定引擎 -------------------------------------------------------------

def _pick_installation(installations, instrument_id, when):
    for inst in installations:
        if inst["instrument_id"] != instrument_id:
            continue
        if parse_ts(inst["start_at"]) <= when <= parse_ts(inst["end_at"]):
            return inst
    return None


def evaluate(store, lot_id, as_of, now, opts=None, pins=None):
    """对一个批次执行测量治理判定。

    pins 用于争议复算：固定读数集合、每支探头使用的证书 / 漂移核查 / 修正曲线，
    保证复算只依赖签署当时采纳的输入，不受事后新增数据影响。
    """
    opts = {**DEFAULT_OPTS, **(opts or {})}
    lot = store.get_lot(lot_id)
    if lot is None:
        raise KeyError(f"批次不存在: {lot_id}")
    segments = store.segments_for_lot(lot_id)
    installations = store.installations_for_lot(lot_id)

    if pins is not None:
        wanted = set(pins["reading_ids"])
        raw_rows = [r for r in store.readings_for_lot(lot_id, as_of)
                    if r["reading_id"] in wanted]
        missing = wanted - {r["reading_id"] for r in raw_rows}
        if missing:
            raise KeyError(f"签署输入中的读数已缺失: {sorted(missing)}")
    else:
        raw_rows = store.readings_for_lot(lot_id, as_of)

    corrected_series = []
    exclusions = []

    def exclude(code, detail, reading_id=None, instrument_id=None):
        exclusions.append({
            "code": code,
            "reading_id": reading_id,
            "instrument_id": instrument_id,
            "detail": detail,
        })

    # 窗口：所有承运区段的并集；无区段时退回读数自身跨度。
    if segments:
        window_start = min(parse_ts(s["start_at"]) for s in segments)
        window_end = max(parse_ts(s["end_at"]) for s in segments)
    elif raw_rows:
        times = sorted(parse_ts(r["device_time"]) for r in raw_rows)
        window_start, window_end = times[0], times[-1]
    else:
        window_start = window_end = parse_ts(as_of)

    inputs = {
        "algorithm_version": ALGORITHM_VERSION,
        "as_of": as_of,
        "opts": opts,
        "window_start": iso(window_start),
        "window_end": iso(window_end),
        "segments": [s["segment_id"] for s in segments],
        "readings": [],
        "certs": {},
        "drifts": {},
        "curves": {},
    }

    accepted = []  # 通过基础门限、进入缺口 / 偏差 / 限值计算的点
    seen_device_times = set()
    for r in raw_rows:
        inst_id = r["instrument_id"]
        instrument = store.get_instrument(inst_id)
        record = {
            "reading_id": r["reading_id"],
            "instrument_id": inst_id,
            "device_time": r["device_time"],
            "received_at": r["received_at"],
            "raw_c": r["raw_c"],
            "backfill": bool(r["backfill"]),
            "exclusions": [],
            "usable": True,
        }
        corrected_series.append(record)
        inputs["readings"].append(r["reading_id"])

        # 0) 同设备同设备时间的重复 / 冲突重传：原始行全部保留，判定只采用
        # 最早接收的一行（SQL 已按 device_time, received_at 排序），其余排除。
        dup_key = (inst_id, r["device_time"])
        if dup_key in seen_device_times:
            record["usable"] = False
            record["exclusions"].append("duplicate_timestamp")
            exclude("duplicate_timestamp",
                    f"设备 {inst_id} 在 {r['device_time']} 存在多次推送，"
                    "采用最早接收的一行", r["reading_id"], inst_id)
            continue
        seen_device_times.add(dup_key)

        # 1) 时钟核查：没有核查记录则时间轴不可信。
        if pins is not None and inst_id in pins["drifts"]:
            drift = store.get_drift(pins["drifts"][inst_id])
        else:
            drift = store.latest_drift(inst_id)
        inputs["drifts"].setdefault(inst_id, drift["drift_id"] if drift else None)
        if drift is None:
            record["usable"] = False
            record["exclusions"].append("clock_unverified")
            exclude("clock_unverified", "该设备无时钟核查记录，无法对齐时间轴",
                    r["reading_id"], inst_id)
            continue
        when, clock_residual_s = normalize_clock(r["device_time"], drift)
        record["normalized_time"] = iso(when)
        record["clock_residual_s"] = round(clock_residual_s, 3)

        # 2) 安装位置 / 所属区段。
        inst = _pick_installation(installations, inst_id, when)
        if inst is None:
            record["usable"] = False
            record["exclusions"].append("outside_installation_window")
            exclude("outside_installation_window",
                    f"读数时刻 {iso(when)} 不在该设备任何登记安装区间内",
                    r["reading_id"], inst_id)
            continue
        record["position"] = inst["position"]
        record["segment_id"] = inst["segment_id"]
        record["nominal_interval_s"] = inst["nominal_interval_s"]

        # 3) 校准证书有效性。
        if pins is not None and inst_id in pins["certs"]:
            cert = store.get_cert(pins["certs"][inst_id])
        else:
            cert = store.cert_valid_at(inst_id, iso(when))
        inputs["certs"].setdefault(inst_id, cert["cert_id"] if cert else None)
        if cert is None:
            record["usable"] = False
            record["exclusions"].append("calibration_expired")
            exclude("calibration_expired",
                    f"设备 {inst_id} 在 {iso(when)} 无有效校准证书",
                    r["reading_id"], inst_id)
            continue

        # 4) 版本化修正曲线（设备级优先，型号级兜底；取读数时刻已发布的最高版本）。
        if pins is not None and inst_id in pins["curves"]:
            pinned_curve_id = pins["curves"][inst_id]
            curve = store.get_curve(pinned_curve_id) if pinned_curve_id else None
        else:
            curve = store.curve_for(inst_id, instrument["model"], iso(when))
        inputs["curves"].setdefault(
            inst_id,
            {"curve_id": curve["curve_id"], "version": curve["version"]} if curve
            else None,
        )
        curve_u = 0.0
        if curve is not None:
            value, curve_u, out_of_range = apply_curve(curve, r["raw_c"])
            record["curve_id"] = curve["curve_id"]
            record["curve_version"] = curve["version"]
            if out_of_range:
                record["curve_out_of_range"] = True
                record["usable"] = False
                record["exclusions"].append("curve_out_of_range")
                exclude("curve_out_of_range",
                        f"读数 {r['raw_c']}℃ 超出曲线 {curve['curve_id']} 标定范围，"
                        "外推修正不可作为履约证据",
                        r["reading_id"], inst_id)
                continue
        else:
            value = float(r["raw_c"])
        record["corrected_c"] = round(value, 4)
        record["uncertainty_c"] = round(
            _combine_uncertainty(cert["max_abs_error_c"], curve_u), 4)
        record["expanded_uncertainty_c"] = round(
            EXPANSION_FACTOR * record["uncertainty_c"], 4)
        accepted.append((record, inst, when))

    # 5) 采样缺口：每支探头在自身安装窗口内做区间覆盖计算。
    per_instrument = {}
    accepted_by_ins = {}
    for record, inst, when in accepted:
        accepted_by_ins.setdefault(record["instrument_id"], []).append(
            (record, inst, when))

    for inst_id, points in accepted_by_ins.items():
        points.sort(key=lambda x: x[2])
        windows = []
        for _, inst, _ in points:
            key = inst["install_id"]
            windows.append((inst, parse_ts(inst["start_at"]),
                            parse_ts(inst["end_at"]), inst["nominal_interval_s"]))
        # 同一安装窗口合并
        uniq = {}
        for inst, s, e, interval in windows:
            uniq[inst["install_id"]] = (s, e, interval)
        covered_s = 0.0
        window_s = 0.0
        gap_s_total = 0.0
        for s, e, interval in uniq.values():
            dur = max((e - s).total_seconds(), 0.0)
            window_s += dur
            half = interval / 2.0
            intervals = []
            for record, _, when in points:
                if not (s <= when <= e):
                    continue
                intervals.append((max(s, when - timedelta(seconds=half)),
                                  min(e, when + timedelta(seconds=half))))
            merged = 0.0
            cs = ce = None
            for a, b in sorted(intervals):
                if cs is None:
                    cs, ce = a, b
                elif a <= ce:
                    ce = max(ce, b)
                else:
                    merged += (ce - cs).total_seconds()
                    gap = (a - ce).total_seconds()
                    if gap > (opts["gap_factor"] - 1) * interval:
                        exclude("sampling_gap",
                                f"设备 {inst_id} 在 {iso(ce)} 至 {iso(a)} 出现 "
                                f"{int(gap)}s 采样缺口",
                                instrument_id=inst_id)
                    gap_s_total += gap
                    cs, ce = a, b
            if cs is not None:
                merged += (ce - cs).total_seconds()
            covered_s += min(merged, dur)
        coverage = round(covered_s / window_s, 4) if window_s else 0.0
        per_instrument[inst_id] = {
            "coverage": coverage,
            "window_s": round(window_s, 1),
            "covered_s": round(covered_s, 1),
            "gap_s": round(max(window_s - covered_s, 0.0), 1),
            "sample_count": len(points),
        }

    # 6) 多探头偏差：同区段时间对齐的读数，偏差超阈值且扩展不确定区间互不重叠。
    by_segment = {}
    for record, inst, when in accepted:
        by_segment.setdefault(record["segment_id"], []).append((record, when))
    divergent_readings = set()
    for seg_id, pts in by_segment.items():
        seg = next(s for s in segments if s["segment_id"] == seg_id)
        tol = timedelta(seconds=seg["nominal_interval_s"] / 2.0)
        pts = sorted(pts, key=lambda x: x[1])
        used = [False] * len(pts)
        for i, (rec_i, t_i) in enumerate(pts):
            if used[i]:
                continue
            cluster = [(rec_i, t_i)]
            used[i] = True
            for j in range(i + 1, len(pts)):
                rec_j, t_j = pts[j]
                if t_j - t_i > tol:
                    break
                if used[j] or rec_j["instrument_id"] == rec_i["instrument_id"]:
                    continue
                cluster.append((rec_j, t_j))
                used[j] = True
            probes = {c[0]["instrument_id"] for c in cluster}
            if len(probes) < 2:
                continue
            hi = max(cluster, key=lambda c: c[0]["corrected_c"])
            lo = min(cluster, key=lambda c: c[0]["corrected_c"])
            r_hi, r_lo = hi[0], lo[0]
            spread = r_hi["corrected_c"] - r_lo["corrected_c"]
            disjoint = ((r_hi["corrected_c"] - r_hi["expanded_uncertainty_c"])
                        > (r_lo["corrected_c"] + r_lo["expanded_uncertainty_c"]))
            if spread > opts["divergence_threshold_c"] and disjoint:
                for rec, t in cluster:
                    divergent_readings.add(rec["reading_id"])
                    rec["usable"] = False
                    if "probe_divergence" not in rec["exclusions"]:
                        rec["exclusions"].append("probe_divergence")
                exclude("probe_divergence",
                        f"区段 {seg_id} 在 {iso(hi[1])} 附近 {len(probes)} 支探头"
                        f"偏差 {spread:.2f}℃，超出 {opts['divergence_threshold_c']}℃"
                        f"且扩展不确定区间互不重叠",
                        instrument_id=seg_id)

    # 7) 限值跨越：以扩展不确定区间判定 exceeded / marginal。
    exceeded_points = []
    marginal_points = []
    for record, inst, when in accepted:
        if record["reading_id"] in divergent_readings:
            continue
        lo_lim, hi_lim = inst["limit_min_c"], inst["limit_max_c"]
        v, u = record["corrected_c"], record["expanded_uncertainty_c"]
        record["limit_status"] = "within"
        if hi_lim is not None:
            if v - u > hi_lim:
                record["limit_status"] = "exceeded"
                exceeded_points.append(record)
            elif v + u > hi_lim:
                record["limit_status"] = "marginal"
                marginal_points.append(record)
        if lo_lim is not None:
            if v + u < lo_lim:
                record["limit_status"] = "exceeded"
                if record not in exceeded_points:
                    exceeded_points.append(record)
            elif v - u < lo_lim and record["limit_status"] == "within":
                record["limit_status"] = "marginal"
                marginal_points.append(record)
        if lo_lim is None and hi_lim is None:
            record["limit_status"] = "no_limit_defined"

    # 8) 汇总结论。
    total_window = sum(v["window_s"] for v in per_instrument.values())
    total_covered = sum(v["covered_s"] for v in per_instrument.values())
    coverage = round(total_covered / total_window, 4) if total_window else 0.0
    blocker_codes = {"calibration_expired", "clock_unverified",
                     "probe_divergence", "curve_out_of_range"}
    hard_blockers = sorted({e["code"] for e in exclusions} & blocker_codes)
    if not accepted:
        hard_blockers.append("no_valid_data")
    if total_window and coverage < opts["min_coverage"]:
        hard_blockers.append("insufficient_coverage")
        exclude("insufficient_coverage",
                f"有效覆盖率 {coverage:.2%} 低于下限 "
                f"{opts['min_coverage']:.2%}",
                instrument_id=lot_id)

    if hard_blockers:
        verdict = "inconclusive"
    elif exceeded_points:
        verdict = "exceeded"
    elif marginal_points:
        verdict = "inconclusive"
    else:
        verdict = "compliant"

    extrema = [r["corrected_c"] for r, _, _ in accepted
               if r["reading_id"] not in divergent_readings]
    summary = {
        "verdict": verdict,
        "window_start": iso(window_start),
        "window_end": iso(window_end),
        "effective_coverage": coverage,
        "min_coverage_required": opts["min_coverage"],
        "per_instrument": per_instrument,
        "min_corrected_c": min(extrema) if extrema else None,
        "max_corrected_c": max(extrema) if extrema else None,
        "exceeded_points": len(exceeded_points),
        "marginal_points": len(marginal_points),
        "blockers": hard_blockers,
        "reading_count": len(raw_rows),
        "backfill_count": sum(1 for r in raw_rows if r["backfill"]),
    }
    return {
        "lot_id": lot_id,
        "as_of": as_of,
        "verdict": verdict,
        "corrected": corrected_series,
        "exclusions": exclusions,
        "inputs": inputs,
        "summary": summary,
        "algorithm_version": ALGORITHM_VERSION,
        "generated_at": now,
    }


# ---- 签署冻结与争议复算 ----------------------------------------------------

def snapshot_digest(content):
    import hashlib
    return hashlib.sha256(canonical_json(content)).hexdigest()


def build_report_snapshot(result):
    """从判定结果构造冻结快照；digest 覆盖除自身外的全部内容。"""
    content = {
        "algorithm_version": result["algorithm_version"],
        "lot_id": result["lot_id"],
        "as_of": result["as_of"],
        "verdict": result["verdict"],
        "summary": result["summary"],
        "corrected": result["corrected"],
        "exclusions": result["exclusions"],
        "inputs": result["inputs"],
        "generated_at": result["generated_at"],
    }
    content["snapshot_digest"] = snapshot_digest(content)
    return content


def create_report(store, lot_id, as_of, now, opts=None):
    """执行判定并冻结为报告；as_of 之后到达的补传数据永远进不了该报告。"""
    result = evaluate(store, lot_id, as_of, now, opts=opts)
    assessment_id = store.add_assessment(
        lot_id, None, as_of, result["summary"]["window_start"],
        result["summary"]["window_end"], result["verdict"], result["corrected"],
        result["exclusions"], result["inputs"], result["summary"], now)
    snapshot = build_report_snapshot(result)
    snapshot["assessment_id"] = assessment_id
    report_id = store.add_report(
        assessment_id, lot_id, None,
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now)
    return report_id, assessment_id, snapshot


def sign_report(store, report_id, signed_by, signed_at):
    report = store.get_report(report_id)
    if report is None:
        raise KeyError(f"报告不存在: {report_id}")
    store.add_signature(report_id, signed_by, signed_at)
    return store.signatures(report_id)


def is_signed(store, report_id):
    return bool(store.signatures(report_id))


def recompute_dispute(store, lot_id, report_id):
    """抽取争议批次复算：只采用签署快照固定的输入，逐项与快照比对。"""
    report = store.get_report(report_id)
    if report is None or report["lot_id"] != lot_id:
        raise KeyError(f"报告不属于批次 {lot_id}: {report_id}")
    snapshot = json.loads(report["snapshot_json"])
    # assessment_id 是摘要落库后赋予的库内标识，不参与签署摘要。
    digest_now = snapshot_digest({k: v for k, v in snapshot.items()
                                  if k not in ("snapshot_digest", "assessment_id")})
    pins = {
        "reading_ids": snapshot["inputs"]["readings"],
        "certs": snapshot["inputs"]["certs"],
        "drifts": snapshot["inputs"]["drifts"],
        # None 是有意固定的“签署时不存在曲线”，不能在复算时改取后发布的新曲线。
        "curves": {k: (v["curve_id"] if v else None)
                   for k, v in snapshot["inputs"]["curves"].items()},
    }
    result = evaluate(
        store, lot_id, snapshot["as_of"], snapshot["generated_at"],
        opts=snapshot["inputs"]["opts"], pins=pins)
    rebuilt = build_report_snapshot(result)
    adopted = [r for r in result["corrected"] if r.get("usable")]
    excluded = [r for r in result["corrected"] if not r.get("usable")]
    uncertainty_bands = [{
        "reading_id": r["reading_id"],
        "instrument_id": r["instrument_id"],
        "corrected_c": r["corrected_c"],
        "interval_c": [round(r["corrected_c"] - r["expanded_uncertainty_c"], 4),
                       round(r["corrected_c"] + r["expanded_uncertainty_c"], 4)],
    } for r in adopted]
    return {
        "lot_id": lot_id,
        "report_id": report_id,
        "signed": is_signed(store, report_id),
        "signatures": store.signatures(report_id),
        "confirmations": store.confirmations(report_id),
        "snapshot_digest": snapshot["snapshot_digest"],
        "snapshot_digest_intact": digest_now == snapshot["snapshot_digest"],
        "recomputed_digest": rebuilt["snapshot_digest"],
        "recompute_matches": digest_now == rebuilt["snapshot_digest"],
        "verdict_snapshot": snapshot["verdict"],
        "verdict_recomputed": result["verdict"],
        "adopted_readings": adopted,
        "excluded_readings": excluded,
        "exclusion_reasons": result["exclusions"],
        "uncertainty_bands": uncertainty_bands,
        "summary": snapshot["summary"],
        "inputs_fixed": snapshot["inputs"],
    }


# ---- 覆盖率比较与轮换 / 复校计划 ------------------------------------------

def compare_coverage(store, dimension):
    """按 route / packaging / season 比较有效数据覆盖率。"""
    if dimension not in ("route_id", "packaging", "season"):
        raise ValueError("dimension 仅支持 route_id / packaging / season")
    latest = {a["lot_id"]: a for a in store.all_latest_assessments()
              if a["segment_id"] is None}
    groups = {}
    for lot in store.all_lots():
        a = latest.get(lot["lot_id"])
        if a is None:
            continue
        key = lot.get(dimension) or "unknown"
        g = groups.setdefault(key, {"group": key, "lots": 0, "coverage_sum": 0.0,
                                    "inconclusive": 0, "exceeded": 0,
                                    "compliant": 0})
        g["lots"] += 1
        g["coverage_sum"] += a["summary_json"]["effective_coverage"]
        g[a["verdict"]] = g.get(a["verdict"], 0) + 1
    out = []
    for g in groups.values():
        g["avg_coverage"] = round(g["coverage_sum"] / g["lots"], 4)
        del g["coverage_sum"]
        out.append(g)
    return sorted(out, key=lambda x: x["avg_coverage"])


def generate_plans(store, now):
    """根据证书有效期、历史覆盖率和漂移情况生成轮换 / 复校计划。"""
    now_dt = parse_ts(now)
    horizon = timedelta(days=DEFAULT_OPTS["recal_horizon_days"])
    created = []
    assessments = store.all_latest_assessments()

    for inst in store.all_instruments():
        inst_id = inst["instrument_id"]
        cert = store.latest_cert(inst_id)
        if cert is None:
            plan_id, is_new = store.add_plan(
                "recalibration", "instrument", inst_id, now,
                "设备从未登记校准证书", now)
            created.append((plan_id, is_new))
        else:
            until = parse_ts(cert["valid_until"])
            if until <= now_dt:
                plan_id, is_new = store.add_plan(
                    "recalibration", "instrument", inst_id,
                    cert["valid_until"], f"校准证书已于 {cert['valid_until']} 过期",
                    now)
                created.append((plan_id, is_new))
            elif until <= now_dt + horizon:
                plan_id, is_new = store.add_plan(
                    "recalibration", "instrument", inst_id,
                    cert["valid_until"],
                    f"校准证书将于 {cert['valid_until']} 到期，提前安排复校", now)
                created.append((plan_id, is_new))

        coverages, divergence, expired = [], False, False
        for a in assessments:
            per = a["summary_json"].get("per_instrument", {})
            if inst_id in per:
                coverages.append(per[inst_id]["coverage"])
            codes = {e["code"] for e in a["exclusions_json"]}
            if "probe_divergence" in codes:
                # 只在涉及该设备时轮换：检查被排除读数归属
                if any(r.get("instrument_id") == inst_id and "probe_divergence"
                       in r.get("exclusions", [])
                       for r in a["corrected_json"]):
                    divergence = True
            if any(r.get("instrument_id") == inst_id and "calibration_expired"
                   in r.get("exclusions", []) for r in a["corrected_json"]):
                expired = True
        drift = store.latest_drift(inst_id)
        fast_drift = drift is not None and abs(drift["ppm"]) >= 20.0
        low_cov = bool(coverages) and max(coverages) < DEFAULT_OPTS["rotation_low_coverage"]
        reasons = []
        if divergence:
            reasons.append("多探头偏差超出可解释范围")
        if low_cov:
            reasons.append(
                f"有效覆盖率最高仅 {max(coverages):.0%}，疑似采样不稳")
        if fast_drift:
            reasons.append(f"时钟漂移 {drift['ppm']} ppm 偏大")
        if expired:
            reasons.append("近期任务中出现校准过期记录")
        if reasons:
            due = iso(now_dt + timedelta(days=14))
            plan_id, is_new = store.add_plan(
                "rotation", "instrument", inst_id, due,
                "；".join(reasons) + "，建议轮换并复检", now)
            created.append((plan_id, is_new))
    return created


# ---- 脱敏证据包 ------------------------------------------------------------

PARTY_ROLE_LABELS = {
    "cooperative": "合作社",
    "carrier": "承运方",
    "cold_storage": "冷库方",
    "lab": "校准实验室",
    "association": "行业协会",
}


def build_evidence_package(store, lot_id, report_id, now):
    """生成理赔证据包：隐去设备序列号、承运商身份、实验室名称等，保留可核验结论。"""
    recomputed = recompute_dispute(store, lot_id, report_id)
    probe_map, party_map = {}, {}

    def pseudonym_probe(inst_id):
        if inst_id not in probe_map:
            probe_map[inst_id] = f"探头{chr(ord('A') + len(probe_map))}"
        return probe_map[inst_id]

    def pseudonym_party(party):
        if party in PARTY_ROLE_LABELS:
            return PARTY_ROLE_LABELS[party]
        if party not in party_map:
            party_map[party] = f"相关方{chr(ord('A') + len(party_map))}"
        return party_map[party]

    def redact_reading(r):
        out = {
            "probe": pseudonym_probe(r["instrument_id"]),
            "device_time": r.get("device_time"),
            "normalized_time": r.get("normalized_time"),
            "raw_c": r["raw_c"],
            "corrected_c": r.get("corrected_c"),
            "expanded_uncertainty_c": r.get("expanded_uncertainty_c"),
            "backfill": r["backfill"],
            "usable": r.get("usable", True),
            "exclusions": r.get("exclusions", []),
            "limit_status": r.get("limit_status"),
            "curve_version": r.get("curve_version"),
        }
        return {k: v for k, v in out.items() if v is not None}

    redacted = {
        "lot_id": lot_id,
        "report_digest": recomputed["snapshot_digest"],
        "algorithm_version": ALGORITHM_VERSION,
        "verdict": recomputed["verdict_snapshot"],
        "summary": {k: v for k, v in recomputed["summary"].items()
                    if k != "per_instrument"},
        "probe_coverage": {pseudonym_probe(k): v for k, v
                           in recomputed["summary"].get("per_instrument", {}).items()},
        "readings": [redact_reading(r)
                     for r in (recomputed["adopted_readings"]
                               + recomputed["excluded_readings"])],
        "exclusion_reasons": [{
            "code": e["code"],
            "detail": e["detail"],
            "probe": pseudonym_probe(e["instrument_id"])
            if e["instrument_id"] in probe_map or e.get("reading_id") else None,
        } for e in recomputed["exclusion_reasons"]],
        "uncertainty_note": f"温度区间为修正值 ±{EXPANSION_FACTOR:.0f} 倍合成标准"
                            f"不确定度（约 95% 置信）",
        "signatures": [{"by": pseudonym_party(s["signed_by"]),
                        "at": s["signed_at"]}
                       for s in recomputed["signatures"]],
        "confirmations": [{
            "party": pseudonym_party(c["party"]),
            "decision": c["decision"],
            "at": c["confirmed_at"],
        } for c in recomputed["confirmations"]],
        "recompute_matches": recomputed["recompute_matches"],
        "redaction": "已隐去设备序列号、承运商 / 实验室名称；映射表不出包",
        "generated_at": now,
    }
    for e in redacted["exclusion_reasons"]:
        if e["probe"] is None:
            del e["probe"]
    package_id = store.add_package(lot_id, report_id, redacted, now)
    return package_id, redacted
