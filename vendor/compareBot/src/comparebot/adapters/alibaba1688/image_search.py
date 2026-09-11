from __future__ import annotations

import asyncio
import io
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from comparebot.domain.models import SearchCandidate


def _boolish(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _price(data: dict[str, Any]) -> Decimal | None:
    value = data.get("priceInfo")
    if not isinstance(value, dict):
        return None
    for key in ("price", "priceUnderLine", "priceInteger"):
        try:
            price = Decimal(str(value.get(key, "")).replace(",", ""))
        except (InvalidOperation, ValueError):
            continue
        if price.is_finite() and price > 0:
            return price
    return None


def _additional_images(data: dict[str, Any], primary: str) -> tuple[str, ...]:
    urls: list[str] = []
    for key in ("imageUrls", "offerPicUrls", "images"):
        values = data.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            url = str(value).strip()
            if url.startswith("//"):
                url = f"https:{url}"
            if url.startswith("https://") and url != primary:
                urls.append(url)
    return tuple(dict.fromkeys(urls))


def parse_candidate(row: dict[str, Any], source_rank: int) -> SearchCandidate | None:
    data = row.get("data") if isinstance(row.get("data"), dict) else row
    offer_id = str(data.get("offerId") or "").strip()
    title = str(data.get("title") or "").strip()
    image_url = str(data.get("offerPicUrl") or data.get("odPicUrl") or "").strip()
    if image_url.startswith("//"):
        image_url = f"https:{image_url}"
    if not offer_id or not title or not image_url.startswith("https://"):
        return None
    try:
        source_score = float(data["normalizationScore"])
    except (KeyError, TypeError, ValueError):
        source_score = None
    return SearchCandidate(
        offer_id=offer_id,
        title=title,
        supplier_id=str(data.get("memberId") or data.get("loginId") or "").strip() or None,
        price_cny=_price(data),
        image_url=image_url,
        offer_url=f"https://detail.1688.com/offer/{offer_id}.html",
        source_rank=source_rank,
        source_score=source_score,
        is_ad=_boolish(data.get("isAd")) or _boolish(data.get("isP4P")),
        additional_image_urls=_additional_images(data, image_url),
    )


class Alibaba1688ImageSearchAdapter:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def search_by_image(self, image: bytes) -> tuple[SearchCandidate, ...]:
        return await asyncio.to_thread(self._search, image)

    def _search(self, image: bytes) -> tuple[SearchCandidate, ...]:
        try:
            from search1688api.sync_session import Sync1688Session
        except ImportError as error:
            raise RuntimeError("install comparebot[search1688]") from error

        with tempfile.TemporaryDirectory(prefix="comparebot-1688-") as directory:
            path = Path(directory) / "query.jpg"
            with Image.open(io.BytesIO(image)) as source:
                ImageOps.exif_transpose(source).convert("RGB").save(path, "JPEG", quality=92)

            session = Sync1688Session(debug=False)
            session.trust_env = False
            original_request = session.request

            def request_with_timeout(method: str, url: str, **kwargs: Any) -> Any:
                kwargs.setdefault("timeout", self._timeout_seconds)
                return original_request(method, url, **kwargs)

            session.request = request_with_timeout
            try:
                rows = session.search_by_image(str(path))
            finally:
                session.close()

        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TypeError("1688 image search returned an invalid payload")
        parsed = tuple(
            candidate
            for rank, row in enumerate(rows, start=1)
            if (candidate := parse_candidate(row, rank)) is not None
        )
        return parsed
