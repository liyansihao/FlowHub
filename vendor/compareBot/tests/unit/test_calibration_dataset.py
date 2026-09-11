import json

import pytest

from comparebot.calibration import dataset


def test_build_manifest_is_balanced_and_deterministic(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dataset, "CATEGORY_LABELS", {"a": "A", "b": "B"})
    for category in ("a", "b"):
        rows = [
            {
                "sku": f"{category}{index}",
                "title": f"title {index}",
                "ozon_image_url": f"https://example.com/{category}{index}.jpg",
                "weight_grams": 100 if index < 4 else None,
            }
            for index in range(6)
        ]
        (tmp_path / f"{category}.json").write_text(
            json.dumps({"category_id": category, "candidates": rows})
        )
    first = dataset.build_manifest(tmp_path, per_category=5, seed=7)
    second = dataset.build_manifest(tmp_path, per_category=5, seed=7)
    assert first == second
    assert first["product_count"] == 10
    assert {row["category_id"] for row in first["products"]} == {"a", "b"}


def test_build_manifest_rejects_short_category(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dataset, "CATEGORY_LABELS", {"a": "A"})
    with pytest.raises(ValueError, match="need 1"):
        dataset.build_manifest(tmp_path, per_category=1)
