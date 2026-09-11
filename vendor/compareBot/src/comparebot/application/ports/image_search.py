from typing import Protocol

from comparebot.domain.models import SearchCandidate


class ImageSearchPort(Protocol):
    async def search_by_image(self, image: bytes) -> tuple[SearchCandidate, ...]: ...


class ImageLoaderPort(Protocol):
    async def load(self, url: str) -> bytes: ...

