import json

from comparebot.calibration.analysis import analyze


def test_analysis_finds_safe_separation(tmp_path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    labels = {}
    for index in range(120):
        product_id = str(index)
        is_match = index % 2 == 0
        score = 0.9 if is_match else 0.4
        case = {
            "source": {"category_id": f"c{index % 6}", "size": "small"},
            "search_and_rank": {
                "candidates": [{"dinov2_similarity": score}],
            },
        }
        (cases / f"{product_id}.json").write_text(json.dumps(case))
        labels[product_id] = {
            "verdict": "match" if is_match else "mismatch",
            "matched_rank": 1 if is_match else None,
        }
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(labels))

    report = analyze(cases, labels_path)

    assert report["labelled_count"] == 120
    assert report["calibration_count"] == 80
    assert report["test_count"] == 40
    assert report["thresholds"]["low_threshold"] > 0.4
    assert report["thresholds"]["high_threshold"] <= 0.9
    assert report["thresholds"]["constraints_met"] is True
    assert report["test_metrics"]["auto_pass_precision"] == 1.0


def test_analysis_reports_best_effort_when_no_safe_threshold_exists(tmp_path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    labels = {}
    for index in range(120):
        product_id = str(index)
        case = {
            "source": {"category_id": f"c{index % 6}", "size": "small"},
            "search_and_rank": {
                "candidates": [{"dinov2_similarity": 0.6}],
            },
        }
        (cases / f"{product_id}.json").write_text(json.dumps(case))
        labels[product_id] = {
            "verdict": "match" if index % 2 == 0 else "mismatch",
            "matched_rank": 1 if index % 2 == 0 else None,
        }
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(labels))

    report = analyze(cases, labels_path)

    assert report["thresholds"]["constraints_met"] is False
    assert report["thresholds"]["selection_metrics"]
