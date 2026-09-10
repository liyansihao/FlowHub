from urllib.parse import urlparse

import httpx


class HttpImageLoader:
    def __init__(self, client: httpx.AsyncClient, *, maximum_bytes: int = 12 * 1024 * 1024) -> None:
        self._client = client
        self._maximum_bytes = maximum_bytes

    async def load(self, url: str) -> bytes:
        host = (urlparse(url).hostname or "").lower()
        referer = "https://www.1688.com/" if host.endswith("alicdn.com") else None
        headers = {"User-Agent": "Mozilla/5.0"}
        if referer:
            headers["Referer"] = referer
        response = await self._client.get(url, headers=headers)
        response.raise_for_status()
        content = response.content
        if not content or len(content) > self._maximum_bytes:
            raise ValueError("image response is empty or too large")
        return content
