from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LabelledScore:
    product_id: str
    category_id: str
    size: str
    score: float
    top1_match: bool
    matched_rank: int | None


def analyze(
    cases_dir: Path,
    labels_path: Path,
    qwen_predictions_path: Path | None = None,
) -> dict[str, Any]:
    records = _records(cases_dir, labels_path)
    if len(records) < 100:
        raise ValueError(f"at least 100 non-uncertain labels are required; found {len(records)}")
    calibration, test = _stratified_split(records)
    thresholds = _choose_thresholds(calibration)
    low, high = thresholds["low_threshold"], thresholds["high_threshold"]
    report = {
        "schema_version": 1,
        "labelled_count": len(records),
        "calibration_count": len(calibration),
        "test_count": len(test),
        "thresholds": thresholds,
        "calibration_metrics": _metrics(calibration, low, high),
        "test_metrics": _metrics(test, low, high),
        "category_metrics": {
            category: _metrics(rows, low, high)
            for category, rows in _group_by_category(test).items()
        },
    }
    if qwen_predictions_path is not None:
        predictions = json.loads(qwen_predictions_path.read_text(encoding="utf-8"))
        qwen_thresholds = _choose_qwen_thresholds(
            calibration,
            predictions,
            low,
            high,
        )
        qwen_pass = qwen_thresholds["match_min_similarity"]
        qwen_reject = qwen_thresholds["mismatch_max_similarity"]
        report["qwen"] = _qwen_metrics(records, predictions, low, high)
        report["qwen_thresholds"] = qwen_thresholds
        report["final_calibration_metrics"] = _pipeline_metrics(
            calibration,
            predictions,
            low,
            high,
            qwen_pass,
            qwen_reject,
        )
        report["final_test_metrics"] = _pipeline_metrics(
            test,
            predictions,
            low,
            high,
            qwen_pass,
            qwen_reject,
        )
        report["final_category_metrics"] = {
            category: _pipeline_metrics(
                rows,
                predictions,
                low,
                high,
                qwen_pass,
                qwen_reject,
            )
            for category, rows in _group_by_category(test).items()
        }
    return report


def _records(cases_dir: Path, labels_path: Path) -> list[LabelledScore]:
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    records = []
    for product_id, label in labels.items():
        if label.get("verdict") == "uncertain":
            continue
        path = cases_dir / f"{product_id}.json"
        if not path.exists():
            continue
        case = json.loads(path.read_text(encoding="utf-8"))
        candidates = case["search_and_rank"]["candidates"]
        if not candidates:
            continue
        matched_rank = label.get("matched_rank") if label.get("verdict") == "match" else None
        records.append(
            LabelledScore(
                product_id=product_id,
                category_id=str(case["source"]["category_id"]),
                size=str(case["source"].get("size") or "unknown"),
                score=float(candidates[0]["dinov2_similarity"]),
                top1_match=matched_rank == 1,
                matched_rank=matched_rank,
            )
        )
    return records


def _stratified_split(
    records: list[LabelledScore],
) -> tuple[list[LabelledScore], list[LabelledScore]]:
    calibration: list[LabelledScore] = []
    test: list[LabelledScore] = []
    groups = _group_by_category(records)
    target = round(len(records) / 3)
    raw = {category: len(rows) * target / len(records) for category, rows in groups.items()}
    quotas = {
        category: min(len(groups[category]) - 1, max(1, math.floor(value)))
        for category, value in raw.items()
    }
    remainder = target - sum(quotas.values())
    order = sorted(
        groups,
        key=lambda category: (raw[category] - quotas[category], category),
        reverse=True,
    )
    for category in order:
        if remainder <= 0:
            break
        if quotas[category] < len(groups[category]) - 1:
            quotas[category] += 1
            remainder -= 1
    for category, rows in sorted(groups.items()):
        ordered = sorted(rows, key=lambda row: _hash(row.product_id))
        test_count = quotas[category]
        test.extend(ordered[:test_count])
        calibration.extend(ordered[test_count:])
    return calibration, test


def _choose_thresholds(records: list[LabelledScore]) -> dict[str, Any]:
    best: tuple[tuple[int, float, float], float, float, dict[str, Any]] | None = None
    fallback: tuple[tuple[float, ...], float, float, dict[str, Any]] | None = None
    for low_step in range(30, 90):
        low = low_step / 100
        for high_step in range(low_step + 1, 100):
            high = high_step / 100
            metrics = _metrics(records, low, high)
            pass_precision = metrics["auto_pass_precision"] or 0.0
            reject_rate = metrics["auto_reject_false_negative_rate"] or 1.0
            fallback_objective = (
                max(0, 10 - metrics["auto_pass_count"])
                + max(0, 10 - metrics["auto_reject_count"]),
                max(0.0, 0.97 - pass_precision) + max(0.0, reject_rate - 0.05),
                max(0.0, 0.97 - pass_precision),
                max(0.0, reject_rate - 0.05),
                -metrics["automated_count"],
            )
            if fallback is None or fallback_objective < fallback[0]:
                fallback = (fallback_objective, low, high, metrics)
            if metrics["auto_pass_count"] < 10 or metrics["auto_reject_count"] < 10:
                continue
            if metrics["auto_pass_precision"] < 0.97:
                continue
            if metrics["auto_reject_false_negative_rate"] > 0.05:
                continue
            objective = (
                metrics["automated_count"],
                metrics["auto_pass_precision"],
                -metrics["auto_reject_false_negative_rate"],
            )
            if best is None or objective > best[0]:
                best = (objective, low, high, metrics)
    selected = best or fallback
    if selected is None:
        raise ValueError("no threshold pair could be evaluated")
    _, low, high, metrics = selected
    return {
        "low_threshold": low,
        "high_threshold": high,
        "constraints_met": best is not None,
        "constraints": {
            "minimum_auto_pass_precision": 0.97,
            "maximum_auto_reject_false_negative_rate": 0.05,
            "minimum_examples_per_automatic_route": 10,
        },
        "selection_metrics": metrics,
    }


def _metrics(records: list[LabelledScore], low: float, high: float) -> dict[str, Any]:
    high_rows = [row for row in records if row.score >= high and row.size == "small"]
    high_manual = [row for row in records if row.score >= high and row.size != "small"]
    low_rows = [row for row in records if row.score < low]
    medium_rows = [row for row in records if low <= row.score < high]
    matches = [row for row in records if row.top1_match]
    high_true = sum(row.top1_match for row in high_rows)
    low_false_negative = sum(row.top1_match for row in low_rows)
    known = len(records)
    return {
        "known_count": known,
        "top1_match_count": len(matches),
        "top1_match_rate": _ratio(len(matches), known),
        "top1_match_rate_ci95": _wilson(len(matches), known),
        "top3_hit_rate": _ratio(sum((row.matched_rank or 99) <= 3 for row in records), known),
        "top5_hit_rate": _ratio(sum(row.matched_rank is not None for row in records), known),
        "auto_pass_count": len(high_rows),
        "auto_pass_precision": _ratio(high_true, len(high_rows)),
        "auto_pass_precision_ci95": _wilson(high_true, len(high_rows)),
        "auto_pass_false_positive_count": len(high_rows) - high_true,
        "auto_reject_count": len(low_rows),
        "auto_reject_false_negative_count": low_false_negative,
        "auto_reject_false_negative_rate": _ratio(low_false_negative, len(matches)),
        "auto_reject_false_negative_rate_ci95": _wilson(
            low_false_negative, len(matches)
        ),
        "medium_count": len(medium_rows),
        "high_large_or_unknown_manual_count": len(high_manual),
        "automated_count": len(high_rows) + len(low_rows),
        "automation_rate_before_qwen": _ratio(len(high_rows) + len(low_rows), known),
    }


def _qwen_metrics(
    records: list[LabelledScore],
    predictions: dict[str, Any],
    low: float,
    high: float,
) -> dict[str, Any]:
    medium = [row for row in records if low <= row.score < high]
    usable = [row for row in medium if predictions.get(row.product_id, {}).get("verdict")]
    correct = sum(
        (predictions[row.product_id]["verdict"] == "match") == row.top1_match
        for row in usable
        if predictions[row.product_id]["verdict"] != "uncertain"
    )
    decisive = [row for row in usable if predictions[row.product_id]["verdict"] != "uncertain"]
    input_tokens = sum(predictions[row.product_id].get("input_tokens") or 0 for row in usable)
    output_tokens = sum(predictions[row.product_id].get("output_tokens") or 0 for row in usable)
    return {
        "medium_count": len(medium),
        "reviewed_count": len(usable),
        "prompt_versions": sorted(
            {
                predictions[row.product_id].get("prompt_version")
                for row in usable
                if predictions[row.product_id].get("prompt_version")
            }
        ),
        "decisive_count": len(decisive),
        "accuracy_when_decisive": _ratio(correct, len(decisive)),
        "uncertain_count": len(usable) - len(decisive),
        "uncertain_rate": _ratio(len(usable) - len(decisive), len(usable)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_cny": round((input_tokens * 1 + output_tokens * 10) / 1_000_000, 6),
        "pricing_assumption": "qwen3-vl-plus Beijing: input CNY 1/M, output CNY 10/M",
    }


def _choose_qwen_thresholds(
    records: list[LabelledScore],
    predictions: dict[str, Any],
    low: float,
    high: float,
) -> dict[str, Any]:
    best: tuple[tuple[float, ...], float, float, dict[str, Any]] | None = None
    fallback: tuple[tuple[float, ...], float, float, dict[str, Any]] | None = None
    low_step = round(low * 100)
    high_step = round(high * 100)
    for pass_step in range(low_step, high_step + 1):
        qwen_pass = pass_step / 100
        for reject_step in range(low_step - 1, high_step):
            qwen_reject = reject_step / 100
            metrics = _pipeline_metrics(
                records,
                predictions,
                low,
                high,
                qwen_pass,
                qwen_reject,
            )
            pass_precision = metrics["approved_precision"] or 0.0
            reject_rate = metrics["false_negative_rate"] or 1.0
            fallback_objective = (
                max(0, 10 - metrics["approved_count"])
                + max(0, 10 - metrics["rejected_count"]),
                max(0.0, 0.97 - pass_precision) + max(0.0, reject_rate - 0.05),
                -metrics["automated_count"],
            )
            if fallback is None or fallback_objective < fallback[0]:
                fallback = (fallback_objective, qwen_pass, qwen_reject, metrics)
            if metrics["approved_count"] < 10 or metrics["rejected_count"] < 10:
                continue
            if pass_precision < 0.97 or reject_rate > 0.05:
                continue
            objective = (
                metrics["automation_rate"],
                metrics["automated_count"],
                pass_precision,
                -reject_rate,
            )
            if best is None or objective > best[0]:
                best = (objective, qwen_pass, qwen_reject, metrics)
    selected = best or fallback
    if selected is None:
        raise ValueError("no Qwen routing threshold pair could be evaluated")
    _, qwen_pass, qwen_reject, metrics = selected
    return {
        "match_min_similarity": qwen_pass,
        "mismatch_max_similarity": qwen_reject,
        "constraints_met": best is not None,
        "selection_metrics": metrics,
    }


def _pipeline_metrics(
    records: list[LabelledScore],
    predictions: dict[str, Any],
    low: float,
    high: float,
    qwen_pass: float,
    qwen_reject: float,
) -> dict[str, Any]:
    outcomes = []
    for row in records:
        if row.score >= high:
            outcome = "approved" if row.size == "small" else "manual"
        elif row.score < low:
            outcome = "rejected"
        else:
            verdict = predictions.get(row.product_id, {}).get("verdict")
            if verdict == "match" and row.score >= qwen_pass:
                outcome = "approved"
            elif verdict == "mismatch" and row.score <= qwen_reject:
                outcome = "rejected"
            else:
                outcome = "manual"
        outcomes.append((row, outcome))
    approved = [row for row, outcome in outcomes if outcome == "approved"]
    rejected = [row for row, outcome in outcomes if outcome == "rejected"]
    true_matches = [row for row in records if row.top1_match]
    approved_true = sum(row.top1_match for row in approved)
    rejected_match = sum(row.top1_match for row in rejected)
    return {
        "known_count": len(records),
        "approved_count": len(approved),
        "approved_precision": _ratio(approved_true, len(approved)),
        "approved_precision_ci95": _wilson(approved_true, len(approved)),
        "false_positive_count": len(approved) - approved_true,
        "rejected_count": len(rejected),
        "false_negative_count": rejected_match,
        "false_negative_rate": _ratio(rejected_match, len(true_matches)),
        "false_negative_rate_ci95": _wilson(rejected_match, len(true_matches)),
        "manual_count": sum(outcome == "manual" for _, outcome in outcomes),
        "manual_rate": _ratio(sum(outcome == "manual" for _, outcome in outcomes), len(records)),
        "automated_count": sum(outcome != "manual" for _, outcome in outcomes),
        "automation_rate": _ratio(
            sum(outcome != "manual" for _, outcome in outcomes), len(records)
        ),
    }


def _group_by_category(
    records: list[LabelledScore],
) -> dict[str, list[LabelledScore]]:
    grouped: dict[str, list[LabelledScore]] = defaultdict(list)
    for row in records:
        grouped[row.category_id].append(row)
    return grouped


def _hash(value: str) -> str:
    return hashlib.sha256(f"comparebot-test:{value}".encode()).hexdigest()


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _wilson(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.96
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * (
        (proportion * (1 - proportion) / total + z * z / (4 * total * total)) ** 0.5
    ) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]
