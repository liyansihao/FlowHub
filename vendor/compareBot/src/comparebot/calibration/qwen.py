from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from comparebot.adapters.qwen.vision_reviewer import PROMPT_VERSION, QwenVisionReviewer
from comparebot.application.ports.vision_reviewer import VisionReviewRequest


async def review_medium_cases(
    cases_dir: Path,
    labels_path: Path,
    analysis_path: Path,
    output_path: Path,
    *,
    api_key: str,
    model: str,
    base_url: str,
    concurrency: int = 2,
) -> dict[str, Any]:
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    thresholds = analysis["thresholds"]
    low = float(thresholds["low_threshold"])
    high = float(thresholds["high_threshold"])
    predictions = _existing(output_path)
    pending = []
    for product_id, label in labels.items():
        existing = predictions.get(product_id, {})
        if label.get("verdict") == "uncertain" or (
            existing.get("prompt_version") == PROMPT_VERSION and "error" not in existing
        ):
            continue
        case_path = cases_dir / f"{product_id}.json"
        if not case_path.exists():
            continue
        case = json.loads(case_path.read_text(encoding="utf-8"))
        candidates = case["search_and_rank"]["candidates"]
        if not candidates:
            continue
        score = float(candidates[0]["dinov2_similarity"])
        if low <= score < high:
            pending.append((product_id, case))

    semaphore = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    async with httpx.AsyncClient(timeout=90, follow_redirects=True, trust_env=False) as client:
        reviewer = QwenVisionReviewer(client, api_key, model=model, base_url=base_url)

        async def run_one(index: int, product_id: str, case: dict[str, Any]) -> None:
            async with semaphore:
                try:
                    review = await reviewer.review(_request(case))
                    result = asdict(review)
                    print(
                        f"[{index}/{len(pending)}] ok {product_id} "
                        f"verdict={review.verdict} elapsed={review.elapsed_ms}ms",
                        flush=True,
                    )
                except Exception as error:
                    result = {
                        "error": f"{type(error).__name__}: {error}",
                        "prompt_version": PROMPT_VERSION,
                    }
                    print(
                        f"[{index}/{len(pending)}] fail {product_id} {type(error).__name__}",
                        flush=True,
                    )
                async with write_lock:
                    predictions[product_id] = result
                    _write_json(output_path, predictions)

        await asyncio.gather(
            *(
                run_one(index, product_id, case)
                for index, (product_id, case) in enumerate(pending, 1)
            )
        )
    return {
        "reviewed": sum(
            row.get("prompt_version") == PROMPT_VERSION and "error" not in row
            for row in predictions.values()
        ),
        "failed": sum(
            row.get("prompt_version") == PROMPT_VERSION and "error" in row
            for row in predictions.values()
        ),
        "pending_this_run": len(pending),
        "prompt_version": PROMPT_VERSION,
    }


def _request(case: dict[str, Any]) -> VisionReviewRequest:
    source = case["source"]
    candidate = case["search_and_rank"]["candidates"][0]["candidate"]
    return VisionReviewRequest(
        source_title=source["title"],
        source_image_urls=(source["image_url"],),
        candidate_title=candidate["title"],
        candidate_image_urls=tuple(
            dict.fromkeys((candidate["image_url"], *candidate.get("additional_image_urls", [])))
        ),
    )


def _existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
