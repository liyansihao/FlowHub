from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

CATEGORY_LABELS = {
    "200001517": "袜子与内衣",
    "17028991": "护肤",
    "17028992": "洗护发",
    "17029018": "本册与手账",
    "17028764": "食品饮料",
    "200001272": "箱包鞋服配件",
    "17027920": "家居清洁",
    "200001483": "图书",
    "17028961": "手工工具",
    "17028962": "针织缝纫工具",
    "17029021": "办公工具",
    "17029023": "文件收纳",
}


def build_manifest(
    source_root: Path,
    *,
    per_category: int = 20,
    seed: int = 20260910,
) -> dict[str, Any]:
    if per_category < 1:
        raise ValueError("per_category must be positive")
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for path in source_root.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        category_id = str(payload.get("category_id") or "")
        if category_id not in CATEGORY_LABELS:
            continue
        rows = payload.get("candidates")
        if not isinstance(rows, list):
            continue
        for row in rows:
            product = _product(row, category_id)
            if product is not None:
                grouped[category_id].setdefault(product["product_id"], product)

    selected: list[dict[str, Any]] = []
    for category_id, category_label in CATEGORY_LABELS.items():
        candidates = list(grouped[category_id].values())
        candidates.sort(key=lambda row: _stable_key(seed, row["product_id"]))
        if len(candidates) < per_category:
            raise ValueError(
                f"category {category_id} has {len(candidates)} usable products; need {per_category}"
            )
        chosen = _prefer_size_mix(candidates, per_category)
        for row in chosen:
            row["category_label"] = category_label
        selected.extend(chosen)

    return {
        "schema_version": 1,
        "seed": seed,
        "per_category": per_category,
        "category_count": len(CATEGORY_LABELS),
        "product_count": len(selected),
        "products": selected,
    }


def _product(row: Any, category_id: str) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    product_id = str(row.get("sku") or "").strip()
    title = str(row.get("title") or "").strip()
    image_url = str(row.get("ozon_image_url") or "").strip()
    if not product_id or not title or not image_url.startswith("https://"):
        return None
    weight = _number(row.get("weight_grams"))
    size = "unknown" if weight is None else "small" if weight <= 400 else "large"
    return {
        "product_id": product_id,
        "title": title,
        "image_url": image_url,
        "product_url": str(
            row.get("ozon_product_url") or f"https://www.ozon.ru/product/{product_id}/"
        ),
        "category_id": category_id,
        "size": size,
        "weight_grams": weight,
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _stable_key(seed: int, product_id: str) -> str:
    return hashlib.sha256(f"{seed}:{product_id}".encode()).hexdigest()


def _prefer_size_mix(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    non_small = [row for row in rows if row["size"] != "small"]
    small = [row for row in rows if row["size"] == "small"]
    non_small_target = min(len(non_small), round(limit * 0.2))
    chosen = non_small[:non_small_target] + small[: limit - non_small_target]
    if len(chosen) < limit:
        used = {row["product_id"] for row in chosen}
        chosen.extend(row for row in rows if row["product_id"] not in used)
    return chosen[:limit]
