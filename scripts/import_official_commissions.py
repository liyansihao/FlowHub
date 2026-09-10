"""Import an already verified official XLSX plus RU Seller taxonomy response; runtime needs neither openpyxl nor ERP."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import openpyxl

p = argparse.ArgumentParser()
p.add_argument("workbook", type=Path)
p.add_argument("taxonomy", type=Path)
p.add_argument("--output", type=Path, default=Path("flowhub/tariffs/commissions.json"))
a = p.parse_args()
w = openpyxl.load_workbook(a.workbook, read_only=True, data_only=True)
s = w["Full ChinaHK"]
assert s["J1"].value == "Starts from 01/12/2025", "Review new effective date before importing"
headers = [s.cell(2, c).value for c in range(11, 17)]
assert headers == [
    "RFBS -> 0 - 1500 -> Тариф, %",
    "RFBS -> 1500.01 - 5000 -> Тариф, %",
    "RFBS -> 5000.01+ -> Тариф, %",
    "FBP -> 0 - 1500 -> Тариф, %",
    "FBP -> 1500.01 - 5000 -> Тариф, %",
    "FBP -> 5000.01+ -> Тариф, %",
]
rows = []
for n, r in enumerate(s.iter_rows(min_row=3, values_only=True), 3):
    assert all(isinstance(v, (float, int)) and 0 <= v <= 1 for v in r[10:16]), f"Invalid rate row {n}"
    rows.append(
        dict(
            id=f"cn-20251201-{n}",
            type_names=[str(x).strip() for x in r[:3]],
            category_names=[str(x).strip() for x in r[3:6]],
            market_names=[str(x).strip() for x in r[6:9]],
            brand=r[9],
            realFBS=[round(x * 100, 5) for x in r[10:13]],
            FBP=[round(x * 100, 5) for x in r[13:16]],
            source_row=n,
        )
    )
taxonomy = {}


def walk(nodes, parents=()):
    for r in nodes:
        if r.get("type_id"):
            taxonomy[str(r["type_id"])] = dict(
                name=r["type_name"],
                category_name=parents[-1]["category_name"],
                category_id=parents[-1]["description_category_id"],
                disabled=r.get("disabled", False) or any(p.get("disabled", False) for p in parents),
            )
        walk(r.get("children", []), parents + (r,))


walk(json.loads(a.taxonomy.read_text())["result"])
out = dict(
    version="ozon-cn-20251201-verified-" + datetime.now(UTC).strftime("%Y%m%d"),
    effective_from="2025-12-01",
    verified_at=datetime.now(UTC).isoformat(),
    region="CHN",
    currency="RUB",
    source_url="https://global-help.ozon.com/zh/commissions/ozon-fees/commissions/?region=CHN",
    download_url="https://cdn.ozone.ru/s3/ozon-disk-api/global-education/ru/commissions/ozon-fees/comissions/Tarifs_CN_01_12_2025_1761720496.xlsx",
    sha256=hashlib.sha256(a.workbook.read_bytes()).hexdigest(),
    rows=rows,
    taxonomy=taxonomy,
    taxonomy_source="https://api-seller.ozon.ru/v1/description-category/tree",
)
a.output.with_suffix(".tmp").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
a.output.with_suffix(".tmp").replace(a.output)
print(f"Imported {len(rows)} official rows and {len(taxonomy)} type IDs")
