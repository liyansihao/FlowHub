import pytest

from comparebot.calibration.labels import load_labels, save_label


def test_labels_are_persisted_by_product(tmp_path) -> None:
    path = tmp_path / "labels.json"
    save_label(path, "123", {"verdict": "match", "matched_rank": 2})
    assert load_labels(path) == {
        "123": {"verdict": "match", "matched_rank": 2, "note": ""}
    }


def test_match_requires_rank(tmp_path) -> None:
    with pytest.raises(ValueError, match="matched_rank"):
        save_label(tmp_path / "labels.json", "123", {"verdict": "match"})
