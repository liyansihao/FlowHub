from decimal import Decimal

from comparebot.adapters.alibaba1688.image_search import parse_candidate


def test_parse_candidate_normalizes_string_flags_and_offer_url() -> None:
    candidate = parse_candidate(
        {
            "data": {
                "offerId": "123",
                "title": "测试商品",
                "memberId": "supplier-1",
                "priceInfo": {"price": "8.50"},
                "offerPicUrl": "//cbu01.alicdn.com/example.jpg",
                "imageUrls": [
                    "//cbu01.alicdn.com/example.jpg",
                    "//cbu01.alicdn.com/example-2.jpg",
                ],
                "linkUrl": "https://tracking.example/click",
                "normalizationScore": "0.75",
                "isAd": "false",
                "isP4P": "true",
            }
        },
        4,
    )

    assert candidate is not None
    assert candidate.offer_id == "123"
    assert candidate.price_cny == Decimal("8.50")
    assert candidate.image_url == "https://cbu01.alicdn.com/example.jpg"
    assert candidate.offer_url == "https://detail.1688.com/offer/123.html"
    assert candidate.source_rank == 4
    assert candidate.source_score == 0.75
    assert candidate.is_ad is True
    assert candidate.additional_image_urls == (
        "https://cbu01.alicdn.com/example-2.jpg",
    )


def test_parse_candidate_rejects_missing_core_fields() -> None:
    assert parse_candidate({"offerId": "123"}, 1) is None
