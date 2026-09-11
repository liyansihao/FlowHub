import json

from comparebot.calibration.collector import summarize_collection


def test_collection_summary_keeps_search_failures_in_denominator(tmp_path) -> None:
    products = [
        {"product_id": "ok", "category_id": "a"},
        {"product_id": "failed", "category_id": "a"},
    ]
    (tmp_path / "ok.json").write_text(
        json.dumps({"search_and_rank": {"candidates": [{"candidate": {}}]}})
    )
    (tmp_path / "failed.error.json").write_text(
        json.dumps({"error": "RuntimeError: 1688 image search returned no candidates"})
    )

    report = summarize_collection(products, tmp_path)

    assert report["attempted"] == 2
    assert report["successful"] == 1
    assert report["coverage_rate"] == 0.5
    assert report["category_coverage"]["a"] == {
        "attempted": 2,
        "successful": 1,
        "failed": 1,
    }
