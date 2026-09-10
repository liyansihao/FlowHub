from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from comparebot.domain.screening import QwenReview


@dataclass(frozen=True)
class VisionReviewRequest:
    source_title: str
    source_image_urls: tuple[str, ...]
    candidate_title: str
    candidate_image_urls: tuple[str, ...]


class VisionReviewerPort(Protocol):
    async def review(self, request: VisionReviewRequest) -> QwenReview: ...
