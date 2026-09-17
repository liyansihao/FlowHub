"""Official unlisting for pinned publication records; zero every observed warehouse.

Only a full zero-stock observation followed by archived readback is completion.
Unknown write outcomes are observed and never replayed automatically.
"""

import time

from . import official_api
from .official_api import OfficialDeferred


async def dispatch(db, key, body, target, marker, api, path, payload):
    from .listing_controls import save

    target[marker] = time.time()
    save(db, key, "running", body)
    try:
        response = await api.post(path, json=payload)
    except OfficialDeferred:
        target.pop(marker, None)
        save(db, key, "running", body)
        raise
    rejected = response.status_code in (401, 403, 429)
    if rejected:
        target.pop(marker, None)
    target[marker + "_http_status"] = response.status_code
    save(db, key, "running", body)
    if rejected:
        duration = (
            300
            if response.status_code in (401, 403)
            else official_api.retry_seconds(response.headers.get("Retry-After"))
        )
        raise OfficialDeferred(
            time.time() + duration,
            "official_authentication" if response.status_code in (401, 403) else "official_rate_limit",
        )
    response.raise_for_status()
    return response


async def remove(db, key, body, record, cfg, keys):
    from .official_status import OzonSellerStatusAdapter

    from .plugin_publication import TestListingJournal

    binding = [str(keys.get("client_id")), str(cfg["warehouse_id"]), str(cfg["shop_id"])]
    if record["account_binding"] != binding:
        raise ValueError("official_account_binding_changed")
    target = body.setdefault(
        "official_target", {"shop_id": str(cfg["shop_id"]), "offer_id": record["offer_id"]}
    )
    if target["shop_id"] != str(cfg["shop_id"]) or target["offer_id"] != record["offer_id"]:
        raise ValueError("official_delist_identity_changed")
    async with official_api.client(db, keys) as api:
        port = OzonSellerStatusAdapter(
            api, shop_id=str(cfg["shop_id"]), warehouse_id=str(cfg["warehouse_id"])
        )
        await port.verify_identity()
        product = await port.find_product(target["shop_id"], target["offer_id"])
        if not product or product.sku == "0":
            return "waiting", body | {"phase": "awaiting_original_import_before_delist"}
        stocks = await port.read_stocks(product)
        if not stocks:
            # Missing rows are not evidence that all sellable warehouses are empty.
            return "waiting", body | {"phase": "awaiting_complete_stock_observation"}
        if any(s.present != 0 or s.reserved != 0 for s in stocks):
            if target.get("zero_dispatched"):
                return "waiting", body | {"phase": "official_zero_stock_readback"}
            payload = {
                "stocks": [
                    {
                        "offer_id": product.offer_id,
                        "product_id": int(product.product_id),
                        "warehouse_id": int(s.warehouse_id),
                        "stock": 0,
                    }
                    for s in stocks
                ]
            }
            target["zero_request"] = payload
            response = await dispatch(
                db, key, body, target, "zero_dispatched", api, "/v2/products/stocks", payload
            )
            target["zero_response"] = response.json()
            return "waiting", body | {"phase": "official_zero_stock_readback"}
        target["zero_stock_verified_at"] = time.time()
        if product.status == "archived":
            target["archived_verified_at"] = time.time()
            # Fence the publisher so an old reconciliation cannot reactivate inventory.
            journal = TestListingJournal(db.directory / "plugin-production.sqlite3")
            previous = journal.read(record["offer_id"])
            journal.move(record["offer_id"], previous["phase"], "manual_review", reason="explicitly_delisted")
            return "unlisted", body | {"phase": "official_archived_verified"}
        if not target.get("archive_dispatched"):
            response = await dispatch(
                db,
                key,
                body,
                target,
                "archive_dispatched",
                api,
                "/v1/product/archive",
                {"product_id": [int(product.product_id)]},
            )
            target["archive_response"] = response.json()
        return "waiting", body | {"phase": "official_archive_readback"}


async def restore(db, key, body, record, cfg, keys):
    import json

    from .official_status import OzonSellerStatusAdapter

    from . import plugin_publication as legacy
    from .pipeline_modules.dossier import require_unchanged_source

    if record["account_binding"] != [
        str(keys.get("client_id")),
        str(cfg["warehouse_id"]),
        str(cfg["shop_id"]),
    ]:
        raise ValueError("official_account_binding_changed")
    with db.connect() as c:
        row = c.execute(
            "SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?", key
        ).fetchone()
        product_row = c.execute(
            "SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?", key
        ).fetchone()
        wf = c.execute("SELECT rules FROM workflows WHERE owner=?", (key[0],)).fetchone()
        if c.execute("SELECT 1 FROM blocks WHERE owner=? AND source_key=?", key[:2]).fetchone():
            raise ValueError("product_blocked")
    review = json.loads(row[0])
    rules = json.loads(wf[0])
    legacy.approved(review, rules)
    require_unchanged_source(review, json.loads(product_row[0]))
    if (
        review["result"]["evidence"]["profit"]["sell_price_cny"]
        != record["review"]["result"]["evidence"]["profit"]["sell_price_cny"]
    ):
        raise ValueError("official_restore_requires_same_approved_price")
    target = body.setdefault(
        "official_target", {"shop_id": str(cfg["shop_id"]), "offer_id": record["offer_id"]}
    )
    if target["shop_id"] != str(cfg["shop_id"]) or target["offer_id"] != record["offer_id"]:
        raise ValueError("official_restore_identity_changed")
    async with official_api.client(db, keys) as api:
        port = OzonSellerStatusAdapter(
            api, shop_id=str(cfg["shop_id"]), warehouse_id=str(cfg["warehouse_id"])
        )
        await port.verify_identity()
        product = await port.find_product(target["shop_id"], target["offer_id"])
        if not product or product.sku == "0":
            raise ValueError("official_restore_product_not_found")
        blocks = await legacy.read_delists()
        offers = {tuple(v) for v in blocks["offers"]}
        if (
            key[1] in blocks["skus"]
            or product.sku in blocks["skus"]
            or (target["shop_id"], product.offer_id) in offers
            or ("*", product.offer_id) in offers
        ):
            raise ValueError("explicit_delist")
        if product.status == "archived":
            if not target.get("restore_dispatched"):
                await dispatch(
                    db,
                    key,
                    body,
                    target,
                    "restore_dispatched",
                    api,
                    "/v1/product/unarchive",
                    {"product_id": [int(product.product_id)]},
                )
            return "waiting", body | {"phase": "official_restore_readback"}
        if product.issue_codes:
            raise ValueError("official_product_has_issues")
        stocks = await port.read_stocks(product)
        if any(s.reserved for s in stocks):
            raise ValueError("reserved_stock_detected")
        target_stock = next((s for s in stocks if s.warehouse_id == str(cfg["warehouse_id"])), None)
        if target_stock and target_stock.present == 99 and product.status == "selling":
            return "listed", body | {"phase": "official_restore_verified"}
        if not target.get("stock_dispatched"):
            await dispatch(
                db,
                key,
                body,
                target,
                "stock_dispatched",
                api,
                "/v2/products/stocks",
                {
                    "stocks": [
                        {
                            "offer_id": product.offer_id,
                            "product_id": int(product.product_id),
                            "warehouse_id": int(cfg["warehouse_id"]),
                            "stock": 99,
                        }
                    ]
                },
            )
        return "waiting", body | {"phase": "official_restore_stock_readback"}
