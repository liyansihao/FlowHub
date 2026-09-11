"""Bounded live probe of 1688 image search using public Ozon product images."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps
from search1688api.sync_session import Sync1688Session


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("fixtures/ozon_samples.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def _download(client: httpx.Client, url: str, destination: Path) -> dict[str, Any]:
    response = client.get(url)
    response.raise_for_status()
    content = response.content
    with Image.open(io.BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        width, height = image.size
        image.save(destination, "JPEG", quality=92)
    return {
        "source_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "width": width,
        "height": height,
        "content_type": response.headers.get("content-type"),
    }


def _boolish(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("data") if isinstance(row.get("data"), dict) else row
    offer_id = str(data.get("offerId") or "")
    offer_url = str(data.get("linkUrl") or "")
    if "detail.1688.com/offer/" not in offer_url and offer_id:
        offer_url = f"https://detail.1688.com/offer/{offer_id}.html"
    return {
        "offer_id": offer_id,
        "title": data.get("title"),
        "supplier_id": data.get("memberId") or data.get("loginId"),
        "price_info": data.get("priceInfo"),
        "image_url": data.get("offerPicUrl") or data.get("odPicUrl"),
        "offer_url": offer_url,
        "is_ad": _boolish(data.get("isAd")),
        "is_p4p": _boolish(data.get("isP4P")),
        "normalization_score": data.get("normalizationScore"),
        "sale_quantity": data.get("saleQuantity"),
        "province": data.get("province"),
    }


def main() -> None:
    args = _args()
    samples = json.loads(args.manifest.read_text(encoding="utf-8"))[: args.limit]
    report: dict[str, Any] = {
        "probe": "1688_image_search_m0",
        "sample_count": len(samples),
        "samples": [],
    }

    client = httpx.Client(
        timeout=args.timeout,
        follow_redirects=True,
        trust_env=False,
        headers={"User-Agent": "compareBot/0.1 M0 read-only probe"},
    )
    session = Sync1688Session(debug=False)
    session.trust_env = False
    original_request = session.request

    def request_with_timeout(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", args.timeout)
        return original_request(method, url, **kwargs)

    session.request = request_with_timeout
    try:
        with tempfile.TemporaryDirectory(prefix="comparebot-m0-") as directory:
            temp = Path(directory)
            for index, sample in enumerate(samples, start=1):
                result: dict[str, Any] = {"input": sample}
                started = time.perf_counter()
                try:
                    image_path = temp / f"ozon-{sample['product_id']}.jpg"
                    download_started = time.perf_counter()
                    result["image"] = _download(client, sample["image_url"], image_path)
                    result["download_seconds"] = round(
                        time.perf_counter() - download_started, 3
                    )
                    search_started = time.perf_counter()
                    rows = session.search_by_image(str(image_path))
                    result["search_seconds"] = round(time.perf_counter() - search_started, 3)
                    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                        raise TypeError("1688 search returned a non-list or non-object row")
                    result["status"] = "completed" if rows else "empty"
                    result["raw_candidate_count"] = len(rows)
                    payloads = [_payload(row) for row in rows]
                    result["unique_offer_count"] = len(
                        {item["offer_id"] for item in payloads if item["offer_id"]}
                    )
                    result["ad_candidate_count"] = sum(item["is_ad"] for item in payloads)
                    result["p4p_candidate_count"] = sum(item["is_p4p"] for item in payloads)
                    data_rows = [
                        row.get("data") if isinstance(row.get("data"), dict) else row
                        for row in rows
                    ]
                    result["available_fields"] = sorted(
                        {key for data in data_rows for key in data}
                    )
                    result["top_candidates"] = payloads[:3]
                except Exception as error:  # Probe records the external failure shape.
                    result["status"] = "error"
                    result["error_type"] = type(error).__name__
                    result["error"] = str(error)[:500]
                result["total_seconds"] = round(time.perf_counter() - started, 3)
                report["samples"].append(result)
                print(
                    f"[{index}/{len(samples)}] {sample['product_id']}: "
                    f"{result['status']} candidates={result.get('raw_candidate_count', 0)} "
                    f"seconds={result['total_seconds']}"
                )
    finally:
        session.close()
        client.close()

    completed = sum(item["status"] == "completed" for item in report["samples"])
    empty = sum(item["status"] == "empty" for item in report["samples"])
    errors = sum(item["status"] == "error" for item in report["samples"])
    report["summary"] = {"completed": completed, "empty": empty, "errors": errors}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
