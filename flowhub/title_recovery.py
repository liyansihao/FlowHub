"""Repair an explicit Latin-title decline once, preserving the original offer and facts."""

import copy
import json
import re
import time

from .modules import ModuleError


def latin_title_decline(errors):
    return bool(errors) and all(
        e.get("code") == "DESCRIPTION_DECLINE"
        and str(e.get("attribute_id")) == "4180"
        and "на латинице" in json.dumps(e, ensure_ascii=False).lower()
        for e in errors
    )


async def recover_title(port, write, errors):
    from .compat import guard
    from .ozon_direct import digest

    pending = {"found": False, "store_id": port.store["id"]}
    blocked = pending | {"issue": True}
    if not latin_title_decline(errors):
        return blocked
    prepared = port.c.get("prepared", {})
    frozen = prepared.get("frozen", {})
    if (
        digest(frozen) != prepared.get("digest")
        or write["payload_hash"] != prepared.get("digest")
        or frozen.get("binding") != port.binding()
    ):
        raise ModuleError("title recovery frozen identity mismatch")
    with port.db.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS direct_title_repairs(offer_id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL,at REAL NOT NULL)"
        )
        prior = db.execute(
            "SELECT state FROM direct_title_repairs WHERE offer_id=?", (port.c["idempotency_key"],)
        ).fetchone()
    if prior:
        # An unacknowledged write may have reached Ozon. Never resubmit it.
        return pending | {"repair": prior["state"]} if prior["state"] == "started" else blocked
    if time.time() - frozen.get("checked_at", 0) > 21600:
        return blocked | {"reason": "title repair requires refreshed price and dossier"}
    item = copy.deepcopy(frozen["item"])
    titles = {
        v["value"].strip()
        for a in item.get("attributes", [])
        if a.get("id") == 4180
        for v in a.get("values", [])
        if isinstance(v.get("value"), str) and re.search(r"[А-Яа-яЁё]", v["value"])
    }
    if len(titles) != 1 or item.get("offer_id") != port.c["idempotency_key"]:
        return blocked | {"reason": "unambiguous original Russian title missing"}
    title = titles.pop()
    if title == item.get("name"):
        return blocked | {"reason": "original Russian title already submitted"}
    product = await port.product()
    if (
        not product
        or not product.get("id")
        or product.get("offer_id") != item["offer_id"]
        or product.get("sku")
        or product.get("is_archived")
        or product.get("statuses", {}).get("is_created") is not False
        or product.get("statuses", {}).get("moderate_status") != "declined"
        or not latin_title_decline(product.get("errors", []))
    ):
        return blocked | {"reason": "product is not an uncreated Latin-title decline"}
    await guard(port.c)
    await port.identity()
    item["name"] = title
    record = {
        "binding": port.binding(),
        "original_task": write["task_id"],
        "product_id": product["id"],
        "errors": errors,
        "request": {"items": [item]},
        "changed_fields": ["name"],
        "title_source": "original attribute 4180",
    }
    with port.db.connect() as db:
        inserted = db.execute(
            "INSERT OR IGNORE INTO direct_title_repairs VALUES(?,?,?,?)",
            (item["offer_id"], port.db.seal(record), "started", time.time()),
        ).rowcount
    if not inserted:
        return pending | {"repair": "started"}
    response = await port.seller("/v3/product/import", record["request"])
    task = (response.get("result") or {}).get("task_id")
    if not task:
        raise ModuleError("title repair acknowledgement missing; readback only")
    record["response"] = response
    with port.db.connect() as db:
        db.execute(
            "UPDATE direct_title_repairs SET state='acknowledged',body=? WHERE offer_id=?",
            (port.db.seal(record), item["offer_id"]),
        )
        db.execute(
            "UPDATE ozon_direct_writes SET task_id=? WHERE offer_id=?",
            (str(task), item["offer_id"]),
        )
    return pending | {"repair": "submitted", "task_id": str(task)}
