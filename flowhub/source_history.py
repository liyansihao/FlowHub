"""Import historical own-listing/source bindings without guessing from offer IDs."""

import json
import re
import sqlite3
from pathlib import Path

from .source_library import identity


def history_files(root):
    root = Path(root)
    files = set()
    for directory in (
        "flow_ef_category_fbs/state",
        "flow_b_ef/state/flow-ef",
        "maozi_playwright_china/state/flow-ef",
        "maozi_playwright_china/low-ticket/state/flow-ef",
        "maozi_playwright_china/lili-low-ticket/state/flow-ef",
    ):
        base = root / directory
        files.update(base.glob("flow-f-publications.jsonl"))
        files.update(base.glob("engines/*/listed.jsonl"))
    for directory in ("ozon_high_profit_direct_ranking", "ozon_high_profit_zero_stock"):
        files.update((root / directory / "state").glob("events.jsonl"))
    for name in ("FlowEF-production/state/production/production.sqlite3",):
        if (root / name).is_file():
            files.add(root / name)
    files.update(root.glob("outputs/profit-selection-*/order-review.json"))
    return sorted(files)


def history_rows(path):
    if path.name == "order-review.json":
        # These rows explicitly link an own offer to a source SKU and listing record.
        # Do not recover source identity by splitting an offer ID.
        try:
            entries = json.loads(path.read_text())
        except ValueError:
            yield 0, None
            return
        if not isinstance(entries, list):
            yield 0, None
            return
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict) or not isinstance(entry.get("fields"), dict):
                yield index, None
                continue
            fields = entry.get("fields", {})
            shop, sku = str(fields.get("店铺ID") or ""), str(fields.get("跟卖源SKU") or "")
            offer = fields.get("本店货号")
            key = str(fields.get("对应上架记录键") or "")
            links = fields.get("对应上架记录") or []
            if not (
                shop.isdigit()
                and sku.isdigit()
                and offer
                and key.startswith(f"{shop}|{sku}|https://www.ozon.ru/product/")
                and re.search(rf"(?:/|-){re.escape(sku)}/?$", key)
                and isinstance(offer, str)
                and isinstance(links, list)
            ):
                continue
            record_ids = [
                rid
                for link in links
                if isinstance(link, dict) and isinstance(link.get("record_ids"), list)
                for rid in link["record_ids"]
                if isinstance(rid, str) and rid.strip()
            ]
            if not record_ids:
                continue
            yield (
                index,
                {
                    "state": "historical_linked_order",
                    "source_sku": sku,
                    "store_id": shop,
                    "offer_id": offer,
                    "own_sku": fields.get("本店Ozon SKU"),
                    "at": fields.get("下单时间"),
                    "linked_record_ids": record_ids,
                    "order_record_id": entry.get("record_id"),
                },
            )
        return
    if path.suffix != ".sqlite3":
        with path.open() as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    yield line_number, row if isinstance(row, dict) else None
                except ValueError:
                    yield line_number, None
        return
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            "SELECT rowid,offer_id,shop_id,sku,plan,phase,updated_at FROM zero_stock_tests WHERE phase='stock_verified'"
        ):
            plan = json.loads(row["plan"])
            yield (
                row["rowid"],
                {
                    "state": "stock_verified",
                    "store_id": row["shop_id"],
                    "source_sku": row["sku"],
                    "offer_id": row["offer_id"],
                    "title": plan.get("title"),
                    "category_id": plan.get("category_id"),
                    "at": row["updated_at"],
                },
            )


def import_history(library, owner, root):
    stats = {"files": 0, "records": 0, "bindings": 0, "conflicts": 0, "malformed": 0, "unresolved_source": 0}
    bindings = {}
    for path in history_files(root):
        stats["files"] += 1
        for line_number, row in history_rows(path):
            if row is None:
                stats["malformed"] += 1
                continue
            publication = row.get("publication") or row.get("publish") or {}
            if row.get("state") not in (
                "submitted",
                "publish_submitted",
                "published_zero_stock",
                "stock_verified",
                "historical_linked_order",
            ):
                continue
            if (
                row["state"] == "published_zero_stock"
                and not row.get("source_sku")
                and not row.get("seller_id")
            ):
                # A stock receipt can contain our own SKU; it is not evidence of the original source.
                stats["unresolved_source"] += 1
                continue
            imported = row.get("import_outcome") or publication.get("import_outcome")
            if row.get("state") in ("submitted", "publish_submitted") and imported != "imported":
                continue
            try:
                shop = identity(row.get("store_id") or (row.get("target") or {}).get("store_id"))
                sku = identity(row.get("source_sku") or row.get("sku"))
            except ValueError:
                continue
            offer = str(row.get("offer_id") or publication.get("offer_id") or "")
            if not offer:
                continue
            stats["records"] += 1
            key = (shop, offer)
            current = bindings.setdefault(
                key,
                {
                    "sku": sku,
                    "title": row.get("title", ""),
                    "evidence": [],
                    "category_id": None,
                    "seller_id": None,
                    "conflict": False,
                },
            )
            if current["sku"] != sku:
                current["conflict"] = True
            for field in ("category_id", "seller_id"):
                if row.get(field):
                    if field == "seller_id" and current[field] and current[field] != str(row[field]):
                        current["conflict"] = True
                    current[field] = str(row[field])
            current["evidence"].append(
                {
                    "file": str(path.relative_to(root)),
                    "line": line_number,
                    "at": row.get("at"),
                    "state": row["state"],
                    "import_outcome": imported,
                    **(
                        {
                            "linked_record_ids": row["linked_record_ids"],
                            "order_record_id": row.get("order_record_id"),
                        }
                        if row.get("linked_record_ids")
                        else {}
                    ),
                }
            )
    # A corrupt log could hide a conflicting binding; do not silently import a partial history.
    if stats["malformed"]:
        return stats | {"blocked": "malformed_history"}
    with library.db.connect() as c:
        for (shop, offer), body in bindings.items():
            if body["conflict"]:
                stats["conflicts"] += 1
                c.execute(
                    "UPDATE sourcing_seeds SET archived=NULL,checked=NULL WHERE owner=? AND shop=? AND offer=?",
                    (owner, shop, offer),
                )
                continue
            existing = c.execute(
                "SELECT sku,body FROM sourcing_seeds WHERE owner=? AND shop=? AND offer=?",
                (owner, shop, offer),
            ).fetchone()
            if existing and existing["sku"] != body["sku"]:
                stats["conflicts"] += 1
                c.execute(
                    "UPDATE sourcing_seeds SET archived=NULL,checked=NULL WHERE owner=? AND shop=? AND offer=?",
                    (owner, shop, offer),
                )
                continue
            if existing:
                prior = json.loads(existing["body"])
                for field in ("online_evidence", "sales_basis"):
                    if field in prior:
                        body[field] = prior[field]
            c.execute(
                """INSERT INTO sourcing_seeds(owner,shop,offer,sku,body) VALUES(?,?,?,?,?)
              ON CONFLICT(owner,shop,offer) DO UPDATE SET body=excluded.body""",
                (owner, shop, offer, body["sku"], json.dumps(body)),
            )
            stats["bindings"] += 1
        for shop in sorted({key[0] for key, body in bindings.items() if not body["conflict"]}):
            library.enqueue(owner, "own_shop", {"shop_id": shop}, 100, c)
            library.enqueue(owner, "own_orders", {"shop_id": shop}, 110, c)
    return stats
