from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class ProductSize(StrEnum):
    SMALL = "small"
    LARGE = "large"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProductQuery:
    product_id: str
    title: str
    image_url: str
    specifications: dict[str, str] = field(default_factory=dict)
    additional_image_urls: tuple[str, ...] = ()
    size: ProductSize = ProductSize.UNKNOWN


@dataclass(frozen=True)
class SearchCandidate:
    offer_id: str
    title: str
    image_url: str
    offer_url: str
    source_rank: int
    supplier_id: str | None = None
    price_cny: Decimal | None = None
    source_score: float | None = None
    is_ad: bool = False
    additional_image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankedCandidate:
    candidate: SearchCandidate
    rank: int
    dinov2_similarity: float


@dataclass(frozen=True)
class SearchAndRankResult:
    query: ProductQuery
    raw_candidate_count: int
    ranked_candidate_count: int
    image_download_failures: int
    model_version: str
    device: str
    candidates: tuple[RankedCandidate, ...]
    timing_ms: dict[str, int]
