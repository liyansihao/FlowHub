from __future__ import annotations

from comparebot.application.ports.vision_reviewer import VisionReviewRequest
from comparebot.application.services.screen_product import ProductScreeningService
from comparebot.domain.models import (
    ProductQuery,
    ProductSize,
    RankedCandidate,
    SearchAndRankResult,
    SearchCandidate,
)
from comparebot.domain.screening import (
    QwenReview,
    QwenVerdict,
    ScreeningOutcome,
    ScreeningPolicy,
    SimilarityTier,
)


def _ranking(similarity: float, size: ProductSize = ProductSize.SMALL) -> SearchAndRankResult:
    query = ProductQuery(
        "ozon-1",
        "Ozon title",
        "https://ozon/main.jpg",
        additional_image_urls=("https://ozon/second.jpg",),
        size=size,
    )
    candidate = SearchCandidate(
        "1688-1",
        "1688 title",
        "https://1688/main.jpg",
        "https://detail.1688/1",
        1,
        additional_image_urls=("https://1688/second.jpg",),
    )
    return SearchAndRankResult(
        query=query,
        raw_candidate_count=1,
        ranked_candidate_count=1,
        image_download_failures=0,
        model_version="fake",
        device="cpu",
        candidates=(RankedCandidate(candidate, 1, similarity),),
        timing_ms={},
    )


class SearchAndRankStub:
    def __init__(self, result: SearchAndRankResult) -> None:
        self.result = result

    async def run(self, query, *, top_k):
        assert query == self.result.query
        assert top_k == 5
        return self.result


class ReviewerStub:
    def __init__(self, verdict: QwenVerdict) -> None:
        self.verdict = verdict
        self.request: VisionReviewRequest | None = None

    async def review(self, request: VisionReviewRequest) -> QwenReview:
        self.request = request
        return QwenReview(self.verdict, 0.9, "test", "fake-qwen")


class FailingReviewer:
    async def review(self, request: VisionReviewRequest) -> QwenReview:
        raise RuntimeError("temporary Qwen failure")


async def _screen(similarity: float, reviewer=None):
    ranking = _ranking(similarity)
    return await ProductScreeningService(
        SearchAndRankStub(ranking),
        reviewer,
        policy=ScreeningPolicy(),
    ).run(ranking.query, top_k=5)


async def test_high_similarity_small_product_is_approved_without_qwen() -> None:
    result = await _screen(0.9)
    assert result.decision.outcome is ScreeningOutcome.APPROVED
    assert result.decision.tier is SimilarityTier.HIGH


async def test_high_similarity_large_product_needs_manual_review() -> None:
    ranking = _ranking(0.9, ProductSize.LARGE)
    result = await ProductScreeningService(SearchAndRankStub(ranking), None).run(
        ranking.query, top_k=5
    )
    assert result.decision.outcome is ScreeningOutcome.MANUAL_REVIEW


async def test_medium_similarity_uses_qwen_and_all_available_images() -> None:
    reviewer = ReviewerStub(QwenVerdict.MATCH)
    result = await _screen(0.84, reviewer)
    assert result.decision.outcome is ScreeningOutcome.APPROVED
    assert reviewer.request is not None
    assert len(reviewer.request.source_image_urls) == 2
    assert len(reviewer.request.candidate_image_urls) == 2


async def test_medium_qwen_mismatch_is_rejected() -> None:
    result = await _screen(0.635, ReviewerStub(QwenVerdict.MISMATCH))
    assert result.decision.outcome is ScreeningOutcome.REJECTED


async def test_qwen_decision_outside_safe_band_needs_manual_review() -> None:
    matched = await _screen(0.75, ReviewerStub(QwenVerdict.MATCH))
    mismatched = await _screen(0.75, ReviewerStub(QwenVerdict.MISMATCH))
    assert matched.decision.outcome is ScreeningOutcome.MANUAL_REVIEW
    assert mismatched.decision.outcome is ScreeningOutcome.MANUAL_REVIEW


async def test_medium_qwen_uncertain_needs_manual_review() -> None:
    result = await _screen(0.75, ReviewerStub(QwenVerdict.UNCERTAIN))
    assert result.decision.outcome is ScreeningOutcome.MANUAL_REVIEW


async def test_medium_qwen_failure_needs_manual_review() -> None:
    result = await _screen(0.75, FailingReviewer())
    assert result.decision.outcome is ScreeningOutcome.MANUAL_REVIEW
    assert result.decision.reason == "qwen_request_failed"


async def test_medium_without_qwen_needs_manual_review() -> None:
    result = await _screen(0.75)
    assert result.decision.outcome is ScreeningOutcome.MANUAL_REVIEW
    assert result.decision.reason == "qwen_not_configured"


async def test_low_similarity_is_rejected_without_qwen() -> None:
    result = await _screen(0.5)
    assert result.decision.outcome is ScreeningOutcome.REJECTED
    assert result.decision.tier is SimilarityTier.LOW


import pytest
from comparebot.domain.screening import decide_without_qwen, decide_from_qwen

@pytest.mark.parametrize("score,size,verdict,expected", [
    (0.629999, "small", "match", "rejected"),
    (0.63, "small", "match", "manual_review"),
    (0.64, "small", "mismatch", "rejected"),
    (0.640001, "small", "mismatch", "manual_review"),
    (0.819999, "small", "match", "manual_review"),
    (0.82, "unknown", "match", "approved"),
    (0.859999, "small", "uncertain", "manual_review"),
    (0.86, "small", "uncertain", "approved"),
    (0.86, "large", "match", "approved"),
    (0.99, "unknown", "mismatch", "manual_review"),
])
async def test_user_policy_boundaries(score, size, verdict, expected):
    ranked = _ranking(score, ProductSize(size))
    reviewer = ReviewerStub(QwenVerdict(verdict))
    result = await ProductScreeningService(SearchAndRankStub(ranked), reviewer).run(ranked.query, top_k=5)
    assert result.decision.outcome == expected
    assert (reviewer.request is None) == (score < 0.63 or (score >= 0.86 and size == "small"))

@pytest.mark.parametrize("score", [0.63, 0.64, 0.82, 0.99])
def test_explicit_brand_model_conflict_overrides_score(score):
    review = QwenReview(QwenVerdict.MISMATCH, .99, "明确型号冲突", "test", brand_or_model_conflict=True)
    assert decide_from_qwen(similarity=score, offer_id="1", policy=ScreeningPolicy(), review=review).outcome == "rejected"
