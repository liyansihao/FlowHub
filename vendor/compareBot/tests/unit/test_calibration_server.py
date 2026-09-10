import json

import pytest

from comparebot.calibration.server import _candidate_image_url


def test_candidate_image_url_is_resolved_from_frozen_case(tmp_path) -> None:
    payload = {
        "search_and_rank": {
            "candidates": [
                {"candidate": {"image_url": "https://example.com/one.jpg"}},
            ]
        }
    }
    (tmp_path / "123.json").write_text(json.dumps(payload))

    assert _candidate_image_url(tmp_path, "123", 1) == "https://example.com/one.jpg"


def test_candidate_image_url_rejects_invalid_product_id(tmp_path) -> None:
    with pytest.raises(ValueError):
        _candidate_image_url(tmp_path, "../secret", 1)
