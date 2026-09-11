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
    high_threshold: float = 0.86
    medium_threshold: float = 0.63
    qwen_match_min_similarity: float = 0.82
    qwen_mismatch_max_similarity: float = 0.64

    def __post_init__(self) -> None:
        if not -1 <= self.medium_threshold < self.high_threshold <= 1:
            raise ValueError("thresholds must satisfy -1 <= medium < high <= 1")
        if not (
            self.medium_threshold
            <= self.qwen_mismatch_max_similarity
            < self.qwen_match_min_similarity
            < self.high_threshold
        ):
            raise ValueError("Qwen thresholds must stay inside the medium similarity band")

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
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_ms: int | None = None
    prompt_version: str | None = None
    brand_or_model_conflict: bool = False

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
        return None
    return None


def decide_from_qwen(
    *,
    similarity: float,
    offer_id: str,
    policy: ScreeningPolicy,
    review: QwenReview,
) -> ScreeningDecision:
    tier = policy.tier(similarity)
    if tier is SimilarityTier.LOW:
        raise ValueError("Qwen review requires similarity >= medium threshold")
    if review.brand_or_model_conflict:
        outcome = ScreeningOutcome.REJECTED
        reason = "qwen_explicit_brand_or_model_conflict"
    elif (
        review.verdict is QwenVerdict.MATCH
        and similarity >= policy.qwen_match_min_similarity
    ):
        outcome = ScreeningOutcome.APPROVED
        reason = "qwen_match_in_safe_band"
    elif (
        review.verdict is QwenVerdict.MISMATCH
        and similarity <= policy.qwen_mismatch_max_similarity
    ):
        outcome = ScreeningOutcome.REJECTED
        reason = "qwen_mismatch_in_safe_band"
    else:
        outcome = ScreeningOutcome.MANUAL_REVIEW
        reason = f"qwen_{review.verdict}_outside_safe_band"
    return ScreeningDecision(
        outcome,
        tier,
        reason,
        offer_id,
        review,
    )
