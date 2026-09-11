from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import httpx
from dotenv import load_dotenv

from comparebot.adapters.alibaba1688.image_search import Alibaba1688ImageSearchAdapter
from comparebot.adapters.dinov2.ranker import DinoV2Ranker
from comparebot.adapters.http_images import HttpImageLoader
from comparebot.adapters.qwen.vision_reviewer import QwenVisionReviewer
from comparebot.application.services.screen_product import ProductScreeningService
from comparebot.application.services.search_and_rank import SearchAndRankService
from comparebot.domain.models import ProductQuery, ProductSize, SearchCandidate, RankedCandidate, SearchAndRankResult
from comparebot.domain.screening import ScreeningPolicy


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the complete compareBot screening flow")
    parser.add_argument("--ranking-input", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=Path("fixtures/ozon_samples.json"))
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--size", choices=tuple(ProductSize), default=None)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--high-threshold", type=float, default=0.86)
    parser.add_argument("--medium-threshold", type=float, default=0.63)
    parser.add_argument("--qwen-match-min-similarity", type=float, default=0.82)
    parser.add_argument("--qwen-mismatch-max-similarity", type=float, default=0.64)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "mps", "cuda"))
    parser.add_argument("--qwen-model", default=os.getenv("QWEN_VL_MODEL", "qwen3-vl-plus"))
    parser.add_argument(
        "--qwen-base-url",
        default=os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
    )
    return parser.parse_args()


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def _extra_images(row: dict) -> tuple[str, ...]:
    values = row.get("additional_image_urls", row.get("image_urls", []))
    if not isinstance(values, list):
        raise ValueError("additional_image_urls/image_urls must be a list")
    primary = str(row["image_url"])
    return tuple(dict.fromkeys(str(value) for value in values if str(value) != primary))


async def _run(args: argparse.Namespace) -> None:
    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    try:
        selected = next(row for row in rows if str(row["product_id"]) == args.product_id)
    except StopIteration as error:
        raise SystemExit(f"product {args.product_id} is not in {args.manifest}") from error

    query = ProductQuery(
        product_id=str(selected["product_id"]),
        title=str(selected["title"]),
        image_url=str(selected["image_url"]),
        specifications=dict(selected.get("specifications") or {}),
        additional_image_urls=_extra_images(selected),
        size=ProductSize(args.size or selected.get("size", ProductSize.UNKNOWN)),
    )
    policy = ScreeningPolicy(
        high_threshold=args.high_threshold,
        medium_threshold=args.medium_threshold,
        qwen_match_min_similarity=args.qwen_match_min_similarity,
        qwen_mismatch_max_similarity=args.qwen_mismatch_max_similarity,
    )
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    ranker = DinoV2Ranker(device=args.device) if args.ranking_input is None else None
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False) as client:
        search_and_rank = SearchAndRankService(
            Alibaba1688ImageSearchAdapter(),
            HttpImageLoader(client),
            ranker,
        )
        if args.ranking_input is not None:
            raw = json.loads(args.ranking_input.read_text(encoding="utf-8"))
            if str(raw["query"]["product_id"]) != query.product_id:
                raise ValueError("cached ranking product mismatch")
            ranked = SearchAndRankResult(**(raw | {
                "query": query,
                "candidates": tuple(RankedCandidate(**(row | {"candidate": SearchCandidate(**row["candidate"])})) for row in raw["candidates"]),
            }))
            class CachedRanking:
                async def run(self, query, *, top_k):
                    return ranked
            search_and_rank = CachedRanking()
        reviewer = (
            QwenVisionReviewer(
                client,
                api_key,
                model=args.qwen_model,
                base_url=args.qwen_base_url,
            )
            if api_key
            else None
        )
        result = await ProductScreeningService(
            search_and_rank,
            reviewer,
            policy=policy,
        ).run(query, top_k=args.top_k)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    top = result.search_and_rank.candidates[0]
    decision = result.decision
    print(
        f"outcome={decision.outcome} tier={decision.tier} "
        f"score={top.dinov2_similarity:.6f} offer={decision.selected_offer_id} "
        f"reason={decision.reason}"
    )


def main() -> None:
    load_dotenv()
    asyncio.run(_run(_args()))


if __name__ == "__main__":
    main()
