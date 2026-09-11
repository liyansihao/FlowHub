import json

import httpx

from comparebot.adapters.qwen.vision_reviewer import QwenVisionReviewer
from comparebot.application.ports.vision_reviewer import VisionReviewRequest
from comparebot.domain.screening import QwenVerdict


async def test_qwen_adapter_sends_titles_and_multiple_images() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                'result: {"verdict":"match","confidence":0.92,'
                                '"reason":"主体结构一致"}'
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 123, "completion_tokens": 45},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        review = await QwenVisionReviewer(client, "test-key").review(
            VisionReviewRequest(
                "Ozon title",
                ("https://ozon/1.jpg", "https://ozon/2.jpg"),
                "1688 title",
                ("https://1688/1.jpg", "https://1688/2.jpg"),
            )
        )

    assert review.verdict is QwenVerdict.MATCH
    assert review.input_tokens == 123
    assert review.output_tokens == 45
    assert review.elapsed_ms is not None
    assert review.prompt_version == "same-product-brand-safe-v3-flowhub-conflict"
    assert review.confidence == 0.92
    content = captured["messages"][0]["content"]
    assert sum(item["type"] == "image_url" for item in content) == 4
    assert "同一个商品本体" in content[0]["text"]
    assert "品牌" in content[0]["text"]
    assert "Ozon title" in content[0]["text"]
    assert "1688 title" in content[0]["text"]


import pytest
from comparebot.adapters.qwen.vision_reviewer import _conflict

@pytest.mark.parametrize("value", ["false", "true", 1, None])
def test_invalid_conflict_flag_not_coerced(value):
    with pytest.raises(ValueError):
        _conflict({"brand_or_model_conflict": value})

def test_conflict_flag_preserved():
    assert _conflict({"brand_or_model_conflict": True}) is True
    assert _conflict({"brand_or_model_conflict": False}) is False
