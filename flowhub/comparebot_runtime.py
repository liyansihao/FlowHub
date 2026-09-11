"""Process-local model reuse for the dedicated continuous worker."""

import asyncio
import json
import sys
from dataclasses import asdict

import httpx

from .comparebot import ROOT, manifest


class WarmScreening:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.ranker = None

    async def screen(self, candidate, api_key="", *, ranking=None):
        vendor = str(ROOT / "vendor/compareBot/src")
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        from comparebot.adapters.alibaba1688.image_search import Alibaba1688ImageSearchAdapter
        from comparebot.adapters.dinov2.ranker import DinoV2Ranker
        from comparebot.adapters.http_images import HttpImageLoader
        from comparebot.adapters.qwen.vision_reviewer import QwenVisionReviewer
        from comparebot.application.services.screen_product import ProductScreeningService
        from comparebot.application.services.search_and_rank import SearchAndRankService
        from comparebot.domain.models import (
            ProductQuery,
            ProductSize,
            RankedCandidate,
            SearchAndRankResult,
            SearchCandidate,
        )

        data = manifest(candidate)
        query = ProductQuery(**(data | {"size": ProductSize(data["size"])}))
        async with self.lock:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False) as client:
                if ranking is None:
                    if self.ranker is None:
                        self.ranker = await asyncio.to_thread(DinoV2Ranker)
                    result = await SearchAndRankService(
                        Alibaba1688ImageSearchAdapter(), HttpImageLoader(client), self.ranker
                    ).run(query)
                    top = result.candidates[0] if result.candidates else None
                    low = top is None or top.dinov2_similarity < 0.63
                    return {
                        "search_and_rank": json.loads(json.dumps(asdict(result), default=str)),
                        "decision": {
                            "outcome": "rejected" if low else "manual_review",
                            "reason": "dinov2_low_similarity" if low else "awaiting_erp_facts",
                            "selected_offer_id": top.candidate.offer_id if top else None,
                        },
                    }
                if str(ranking["query"]["product_id"]) != query.product_id:
                    raise ValueError("ranking product mismatch")
                cached = SearchAndRankResult(
                    **(
                        ranking
                        | {
                            "query": query,
                            "candidates": tuple(
                                RankedCandidate(**(r | {"candidate": SearchCandidate(**r["candidate"])}))
                                for r in ranking["candidates"]
                            ),
                        }
                    )
                )

                class Replay:
                    async def run(self, query, **kwargs):
                        return cached

                reviewer = QwenVisionReviewer(client, api_key) if api_key else None
                return json.loads(
                    json.dumps(
                        asdict(await ProductScreeningService(Replay(), reviewer).run(query)), default=str
                    )
                )
