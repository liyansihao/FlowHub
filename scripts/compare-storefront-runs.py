"""Export independent timed-run evidence and compare with the UI-assisted baseline."""

import argparse
import csv
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--run", type=Path, required=True)
p.add_argument("--baseline", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
run = json.loads((a.run / "summary.json").read_text())
export = json.loads((a.run / "export.json").read_text())
old = json.loads(a.baseline.read_text())
a.output.mkdir(parents=True, exist_ok=True)
all_products = {row["sku"]: row for row in export["products"]}
round_skus = {
    sku
    for attempt in export["attempts"]
    if attempt["state"] == "committed" and attempt["at"] >= run["started_at"]
    for sku in attempt["body"]["skus"]
}
products = {sku: row for sku, row in all_products.items() if sku in round_skus}
comparison = dict(
    baseline=old,
    independent=run,
    unique_skus=len(products),
    accumulated_store_skus=len(all_products),
    page_rate_ratio=(run["committed_pages"] / run["elapsed_seconds"])
    / (old["round_pages"] / old["elapsed_seconds"]),
    sku_rate_ratio=(run["added_skus"] / run["elapsed_seconds"])
    / (old["round_added_skus"] / old["elapsed_seconds"]),
    whole_shop_complete=run["task"]["state"] == "done",
    qualification="discovery_only",
)
(a.output / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2))
(a.output / "products.json").write_text(json.dumps(list(products.values()), ensure_ascii=False, indent=2))
with (a.output / "products.csv").open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(
        f, fieldnames=["sku", "seller_id", "title", "url", "current_price_display", "collected_at"]
    )
    writer.writeheader()
    for item in products.values():
        writer.writerow({k: item.get(k) for k in writer.fieldnames})
print(
    json.dumps(
        {k: v for k, v in comparison.items() if k not in ("baseline", "independent")},
        ensure_ascii=False,
        indent=2,
    )
)
