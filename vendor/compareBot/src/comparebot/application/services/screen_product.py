from __future__ import annotations

from dataclasses import dataclass

from comparebot.application.ports.vision_reviewer import (
    VisionReviewerPort,
    VisionReviewRequest,
)
from comparebot.application.services.search_and_rank import SearchAndRankService
from comparebot.domain.models import ProductQuery, SearchAndRankResult
from comparebot.domain.screening import (
    ScreeningDecision,
    ScreeningOutcome,
    ScreeningPolicy,
    decide_from_qwen,
    decide_without_qwen,
)


@dataclass(frozen=True)
class ProductScreeningResult:
    search_and_rank: SearchAndRankResult
    decision: ScreeningDecision


class ProductScreeningService:
    def __init__(
        self,
        search_and_rank: SearchAndRankService,
        reviewer: VisionReviewerPort | None,
        *,
        policy: ScreeningPolicy | None = None,
    ) -> None:
        self._search_and_rank = search_and_rank
        self._reviewer = reviewer
        self._policy = policy or ScreeningPolicy()

    async def run(self, query: ProductQuery, *, top_k: int = 10) -> ProductScreeningResult:
        ranked = await self._search_and_rank.run(query, top_k=top_k)
        if not ranked.candidates:
            raise RuntimeError("DINOv2 returned no ranked candidates")

        selected = ranked.candidates[0]
        similarity = selected.dinov2_similarity
        offer_id = selected.candidate.offer_id
        decision = decide_without_qwen(
            similarity=similarity,
            product_size=query.size,
            offer_id=offer_id,
            policy=self._policy,
        )
        if decision is not None:
            return ProductScreeningResult(ranked, decision)

        if self._reviewer is None:
            return ProductScreeningResult(
                ranked,
                ScreeningDecision(
                    ScreeningOutcome.MANUAL_REVIEW,
                    self._policy.tier(similarity),
                    "qwen_not_configured",
                    offer_id,
                ),
            )

        candidate = selected.candidate
        request = VisionReviewRequest(
            source_title=query.title,
            source_image_urls=_images(query.image_url, query.additional_image_urls),
            candidate_title=candidate.title,
            candidate_image_urls=_images(
                candidate.image_url,
                candidate.additional_image_urls,
            ),
        )
        try:
            review = await self._reviewer.review(request)
        except Exception:
            return ProductScreeningResult(
                ranked,
                ScreeningDecision(
                    ScreeningOutcome.MANUAL_REVIEW,
                    self._policy.tier(similarity),
                    "qwen_request_failed",
                    offer_id,
                ),
            )
        return ProductScreeningResult(
            ranked,
            decide_from_qwen(
                similarity=similarity,
                offer_id=offer_id,
                policy=self._policy,
                review=review,
            ),
        )


def _images(primary: str, additional: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((primary, *additional)))
