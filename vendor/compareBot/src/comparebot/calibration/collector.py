from __future__ import annotations

import json
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from comparebot.adapters.alibaba1688.image_search import Alibaba1688ImageSearchAdapter
from comparebot.adapters.dinov2.ranker import DinoV2Ranker
from comparebot.adapters.http_images import HttpImageLoader
from comparebot.application.services.search_and_rank import SearchAndRankService
from comparebot.domain.models import ProductQuery, ProductSize


async def collect_cases(
    manifest_path: Path,
    cases_dir: Path,
    *,
    top_k: int = 5,
    device: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    products = manifest["products"][:limit]
    cases_dir.mkdir(parents=True, exist_ok=True)
    ranker = DinoV2Ranker(device=device)
    completed = skipped = failed = 0
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False) as client:
        service = SearchAndRankService(
            Alibaba1688ImageSearchAdapter(),
            HttpImageLoader(client),
            ranker,
        )
        for index, row in enumerate(products, start=1):
            destination = cases_dir / f"{row['product_id']}.json"
            if _is_complete(destination):
                skipped += 1
                print(f"[{index}/{len(products)}] skip {row['product_id']}", flush=True)
                continue
            started = time.perf_counter()
            query = ProductQuery(
                product_id=row["product_id"],
                title=row["title"],
                image_url=row["image_url"],
                size=ProductSize(row["size"]),
            )
            try:
                result = await service.run(query, top_k=top_k)
                payload = {
                    "schema_version": 1,
                    "source": row,
                    "search_and_rank": asdict(result),
                }
                _write_json(destination, payload)
                completed += 1
                score = result.candidates[0].dinov2_similarity
                print(
                    f"[{index}/{len(products)}] ok {row['product_id']} "
                    f"score={score:.6f} elapsed={time.perf_counter() - started:.1f}s",
                    flush=True,
                )
            except Exception as error:
                failed += 1
                payload = {
                    "schema_version": 1,
                    "source": row,
                    "error": f"{type(error).__name__}: {error}",
                }
                _write_json(cases_dir / f"{row['product_id']}.error.json", payload)
                print(
                    f"[{index}/{len(products)}] fail {row['product_id']} "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )
    return {"completed": completed, "skipped": skipped, "failed": failed}


def _is_complete(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("search_and_rank", {}).get("candidates"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")
