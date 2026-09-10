from __future__ import annotations

import asyncio
import time

from comparebot.application.ports.image_ranker import ImageRankerPort, RankableImage
from comparebot.application.ports.image_search import ImageLoaderPort, ImageSearchPort
from comparebot.domain.models import ProductQuery, RankedCandidate, SearchAndRankResult


class SearchAndRankService:
    def __init__(
        self,
        search: ImageSearchPort,
        images: ImageLoaderPort,
        ranker: ImageRankerPort,
        *,
        download_concurrency: int = 12,
    ) -> None:
        if download_concurrency < 1:
            raise ValueError("download_concurrency must be positive")
        self._search = search
        self._images = images
        self._ranker = ranker
        self._download_concurrency = download_concurrency

    async def run(self, query: ProductQuery, *, top_k: int = 10) -> SearchAndRankResult:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        started = time.perf_counter()
        reference = await self._images.load(query.image_url)

        search_started = time.perf_counter()
        found = await self._search.search_by_image(reference)
        search_ms = _milliseconds(search_started)
        unique = {candidate.offer_id: candidate for candidate in found}
        if not unique:
            raise RuntimeError("1688 image search returned no candidates")

        download_started = time.perf_counter()
        semaphore = asyncio.Semaphore(self._download_concurrency)

        async def download(candidate):
            async with semaphore:
                try:
                    content = await self._images.load(candidate.image_url)
                except Exception:
                    return candidate, None
                return candidate, content

        downloaded = await asyncio.gather(*(download(item) for item in unique.values()))
        usable = [(candidate, content) for candidate, content in downloaded if content is not None]
        download_ms = _milliseconds(download_started)
        if not usable:
            raise RuntimeError("no 1688 candidate image could be downloaded")

        ranking_started = time.perf_counter()
        scores = await asyncio.to_thread(
            self._ranker.rank,
            reference,
            [RankableImage(candidate.offer_id, content) for candidate, content in usable],
        )
        ranking_ms = _milliseconds(ranking_started)
        by_id = {candidate.offer_id: candidate for candidate, _ in usable}
        ranked = tuple(
            RankedCandidate(
                candidate=by_id[score.candidate_id],
                rank=score.rank,
                dinov2_similarity=score.similarity,
            )
            for score in scores[:top_k]
        )
        return SearchAndRankResult(
            query=query,
            raw_candidate_count=len(found),
            ranked_candidate_count=len(scores),
            image_download_failures=len(unique) - len(usable),
            model_version=self._ranker.model_version,
            device=self._ranker.device,
            candidates=ranked,
            timing_ms={
                "image_search": search_ms,
                "candidate_download": download_ms,
                "dinov2_ranking": ranking_ms,
                "total": _milliseconds(started),
            },
        )


def _milliseconds(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
