from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import httpx

from comparebot.adapters.alibaba1688.image_search import Alibaba1688ImageSearchAdapter
from comparebot.adapters.dinov2.ranker import DinoV2Ranker
from comparebot.adapters.http_images import HttpImageLoader
from comparebot.application.services.search_and_rank import SearchAndRankService
from comparebot.domain.models import ProductQuery


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search 1688 and rerank with local DINOv2")
    parser.add_argument("--manifest", type=Path, default=Path("fixtures/ozon_samples.json"))
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "mps", "cuda"))
    return parser.parse_args()


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


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
    )
    ranker = DinoV2Ranker(device=args.device)
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        trust_env=False,
        proxy=os.environ.get("FLOWHUB_REVIEW_IMAGE_PROXY") or None,
    ) as client:
        service = SearchAndRankService(
            Alibaba1688ImageSearchAdapter(),
            HttpImageLoader(client),
            ranker,
        )
        result = await service.run(query, top_k=args.top_k)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(
        f"ranked={result.ranked_candidate_count} top_k={len(result.candidates)} "
        f"device={result.device} total_ms={result.timing_ms['total']}"
    )
    for item in result.candidates:
        print(
            f"#{item.rank} score={item.dinov2_similarity:.6f} "
            f"offer={item.candidate.offer_id} {item.candidate.title}"
        )


def main() -> None:
    asyncio.run(_run(_args()))


if __name__ == "__main__":
    main()
