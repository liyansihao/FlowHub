from __future__ import annotations

import json
import time
from typing import Any

import httpx

from comparebot.application.ports.vision_reviewer import VisionReviewRequest
from comparebot.domain.screening import QwenReview, QwenVerdict

PROMPT_VERSION = "same-product-brand-safe-v3-flowhub-conflict"


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
        started = time.perf_counter()
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
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
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
                input_tokens=_integer(usage.get("prompt_tokens")),
                output_tokens=_integer(usage.get("completion_tokens")),
                elapsed_ms=round((time.perf_counter() - started) * 1000),
                prompt_version=PROMPT_VERSION,
                brand_or_model_conflict=_conflict(data),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Qwen returned an invalid review verdict") from error

    def _content(self, request: VisionReviewRequest) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "判断1688候选和Ozon原商品是否是同一个商品本体。"
                    "用途或品类相同但不是同一个东西，必须判为mismatch。"
                    "若两边明确展示的品牌或型号冲突，也必须判为mismatch；"
                    "一边没有品牌或品牌看不清，不算品牌冲突。"
                    "颜色、拍摄角度、背景、外包装或销售件数不同，不单独否决。"
                    "能确认是同一个东西且无明确品牌错误时判match；"
                    "商品本体不同判mismatch，看不清是否同一个东西时判uncertain。"
                    "只返回JSON："
                    '{"brand_or_model_conflict":明确品牌或型号冲突时为true否则false,'
                    '"verdict":"match|mismatch|uncertain",'
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
            {"type": "image_url", "image_url": {"url": url}} for url in urls[: self._max_images]
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


def _integer(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _conflict(data: dict) -> bool:
    value = data.get("brand_or_model_conflict", False)
    if not isinstance(value, bool):
        raise ValueError("brand_or_model_conflict must be boolean")
    return value
