from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from comparebot.domain.models import ProductSize


class SimilarityTier(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ScreeningOutcome(StrEnum):
    APPROVED = "approved"
    MANUAL_REVIEW = "manual_review"
    REJECTED = "rejected"


class QwenVerdict(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ScreeningPolicy:
    high_threshold: float = 0.75
    medium_threshold: float = 0.55

    def __post_init__(self) -> None:
        if not -1 <= self.medium_threshold < self.high_threshold <= 1:
            raise ValueError("thresholds must satisfy -1 <= medium < high <= 1")

    def tier(self, similarity: float) -> SimilarityTier:
        if similarity >= self.high_threshold:
            return SimilarityTier.HIGH
        if similarity >= self.medium_threshold:
            return SimilarityTier.MEDIUM
        return SimilarityTier.LOW


@dataclass(frozen=True)
class QwenReview:
    verdict: QwenVerdict
    confidence: float
    reason: str
    model: str

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class ScreeningDecision:
    outcome: ScreeningOutcome
    tier: SimilarityTier
    reason: str
    selected_offer_id: str
    qwen_review: QwenReview | None = None


def decide_without_qwen(
    *,
    similarity: float,
    product_size: ProductSize,
    offer_id: str,
    policy: ScreeningPolicy,
) -> ScreeningDecision | None:
    tier = policy.tier(similarity)
    if tier is SimilarityTier.LOW:
        return ScreeningDecision(
            ScreeningOutcome.REJECTED,
            tier,
            "dinov2_low_similarity",
            offer_id,
        )
    if tier is SimilarityTier.HIGH:
        if product_size is ProductSize.SMALL:
            return ScreeningDecision(
                ScreeningOutcome.APPROVED,
                tier,
                "dinov2_high_small_product",
                offer_id,
            )
        return ScreeningDecision(
            ScreeningOutcome.MANUAL_REVIEW,
            tier,
            "dinov2_high_large_or_unknown_size",
            offer_id,
        )
    return None


def decide_from_qwen(
    *,
    similarity: float,
    offer_id: str,
    policy: ScreeningPolicy,
    review: QwenReview,
) -> ScreeningDecision:
    tier = policy.tier(similarity)
    if tier is not SimilarityTier.MEDIUM:
        raise ValueError("Qwen review is only valid for medium similarity")
    outcomes = {
        QwenVerdict.MATCH: ScreeningOutcome.APPROVED,
        QwenVerdict.MISMATCH: ScreeningOutcome.REJECTED,
        QwenVerdict.UNCERTAIN: ScreeningOutcome.MANUAL_REVIEW,
    }
    return ScreeningDecision(
        outcomes[review.verdict],
        tier,
        f"qwen_{review.verdict}",
        offer_id,
        review,
    )
