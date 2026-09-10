from __future__ import annotations

import json
from typing import Any

import httpx

from comparebot.application.ports.vision_reviewer import VisionReviewRequest
from comparebot.domain.screening import QwenReview, QwenVerdict


class QwenVisionReviewer:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        model: str = "qwen3-vl-plus",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_images_per_product: int = 4,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key is required")
        if max_images_per_product < 1:
            raise ValueError("max_images_per_product must be positive")
        self._client = client
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_images = max_images_per_product

    async def review(self, request: VisionReviewRequest) -> QwenReview:
        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "temperature": 0,
                "max_tokens": 400,
                "messages": [
                    {
                        "role": "user",
                        "content": self._content(request),
                    }
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        try:
            answer = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("Qwen returned an invalid response") from error
        if not isinstance(answer, str):
            raise ValueError("Qwen response content is not text")
        data = _json_object(answer)
        try:
            return QwenReview(
                verdict=QwenVerdict(str(data["verdict"]).lower()),
                confidence=float(data["confidence"]),
                reason=str(data["reason"]).strip(),
                model=self._model,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Qwen returned an invalid review verdict") from error

    def _content(self, request: VisionReviewRequest) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "判断两组图片和标题是否代表同一种可替代销售的商品。"
                    "重点检查主体用途、外形结构、关键部件、图案、数量和配件；"
                    "不要因为背景、角度、颜色或包装不同就判为不同款。"
                    "若证据不足必须返回 uncertain。"
                    "只返回JSON："
                    '{"verdict":"match|mismatch|uncertain",'
                    '"confidence":0到1之间的数字,"reason":"简短中文理由"}。\n'
                    f"Ozon标题：{request.source_title}\n"
                    f"1688标题：{request.candidate_title}"
                ),
            },
            {"type": "text", "text": "以下是Ozon商品图片："},
        ]
        content.extend(self._images(request.source_image_urls))
        content.append({"type": "text", "text": "以下是1688候选商品图片："})
        content.extend(self._images(request.candidate_image_urls))
        return content

    def _images(self, urls: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            {"type": "image_url", "image_url": {"url": url}}
            for url in urls[: self._max_images]
        ]


def _json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Qwen response does not contain a JSON object")
