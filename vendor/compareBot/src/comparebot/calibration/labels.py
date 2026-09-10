from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERDICTS = {"match", "mismatch", "uncertain"}


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("labels file must contain a JSON object")
    return payload


def save_label(path: Path, product_id: str, label: dict[str, Any]) -> None:
    verdict = str(label.get("verdict") or "")
    if verdict not in VERDICTS:
        raise ValueError("verdict must be match, mismatch, or uncertain")
    matched_rank = label.get("matched_rank")
    if verdict == "match" and matched_rank not in range(1, 6):
        raise ValueError("match requires matched_rank between 1 and 5")
    if verdict != "match":
        matched_rank = None
    labels = load_labels(path)
    labels[product_id] = {
        "verdict": verdict,
        "matched_rank": matched_rank,
        "note": str(label.get("note") or "").strip(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(labels, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
