from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RankableImage:
    candidate_id: str
    content: bytes


@dataclass(frozen=True)
class ImageScore:
    candidate_id: str
    similarity: float
    rank: int


class ImageRankerPort(Protocol):
    @property
    def model_version(self) -> str: ...

    @property
    def device(self) -> str: ...

    def rank(
        self,
        reference: bytes,
        candidates: Sequence[RankableImage],
    ) -> tuple[ImageScore, ...]: ...
