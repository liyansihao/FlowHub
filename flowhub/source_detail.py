"""Source-card acquisition and deterministic mapping; publication is a separate module."""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from .modules import ModuleError, Pending


async def request(path, method, keys, query=None, body=None):
    token = keys.get("erp_token")
    if not token:
        raise ModuleError("source detail requires an ERP data connection")
    bridge = Path(__file__).resolve().parent.parent / "bridges/source-detail.mjs"
    process = await asyncio.create_subprocess_exec(
        "node",
        str(bridge),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ | {"MAOZI_ACCESS_TOKEN": token},
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(
                json.dumps({"path": path, "method": method, "query": query, "body": body}).encode()
            ),
            95,
        )
        data = json.loads(output)
        if not data.get("ok"):
            raise ModuleError("source acquisition failed; submission state retained")
        return data["data"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class SourceCollector:
    def __init__(self, db, context):
        self.db, self.c = db, context
        self.keys = context["store"]["credentials"]
        self.sku = str(context["candidate"]["source_key"])
        if not self.sku.isdigit():
            raise ModuleError("source acquisition requires numeric Ozon SKU")
        self.key = hashlib.sha256(
            (
                context["owner"]
                + ":"
                + hashlib.sha256(self.keys.get("erp_token", "").encode()).hexdigest()
                + ":"
                + self.sku
            ).encode()
        ).hexdigest()
        with db.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS source_details(key TEXT PRIMARY KEY,state TEXT NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL)"
            )

    async def call(self, path, method="GET", query=None, body=None):
        return await request(path, method, self.keys, query, body)

    def load(self):
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM source_details WHERE key=?", (self.key,)).fetchone()
        return (row["state"], self.db.open(row["body"]), row["updated"]) if row else ("new", {}, 0)

    def save(self, state, data):
        with self.db.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO source_details VALUES(?,?,?,?)",
                (self.key, state, self.db.seal(data), time.time()),
            )

    async def collect(self):
        # Claim once per source/account. A crashed collecting call is not repeated blindly.
        with self.db.connect() as db:
            inserted = (
                db.execute(
                    "INSERT OR IGNORE INTO source_details VALUES(?,?,?,?)",
                    (self.key, "claimed", self.db.seal({}), time.time()),
                ).rowcount
                == 1
            )
        state, data, updated = self.load()
        if state == "ready" and time.time() - updated < 21600:
            return data
        if data.get("draft_id"):
            detail = await self.call(
                "/api.product.collect/detail", query={"id": data["draft_id"], "is_online": 0}
            )
            data.pop("mapping", None)
            data.pop("mapping_client", None)
            data |= {"detail": detail, "observed_at": time.time()}
            self.save("ready", data)
            return data
        if not inserted and state in ("claimed", "favorite_started") and time.time() - updated > 120:
            # A read may be retried after its bounded subprocess timeout. A favorite write
            # is recovered by lookup only; the write itself is never replayed.
            if state == "favorite_started":
                data["favorite_attempted"] = True
            with self.db.connect() as db:
                inserted = (
                    db.execute(
                        "UPDATE source_details SET state='claimed',body=?,updated=? WHERE key=? AND state=? AND updated=?",
                        (self.db.seal(data), time.time(), self.key, state, updated),
                    ).rowcount
                    == 1
                )
        if not inserted:
            raise Pending("source draft outcome unresolved; not creating another draft")
        try:
            favorites = await self.call(
                "/api.product.favorite/lists", query={"sku": self.sku, "page": 1, "page_size": 100}
            )
        except BaseException:
            if data.get("favorite_attempted"):
                self.save("favorite_started", data)
            else:
                with self.db.connect() as db:
                    db.execute("DELETE FROM source_details WHERE key=? AND state='claimed'", (self.key,))
            raise
        rows = favorites if isinstance(favorites, list) else favorites.get("data", [])
        matches = [r for r in rows if str(r.get("sku")) == self.sku]
        if len(matches) > 1:
            raise ModuleError("ambiguous source favorite")
        if not matches:
            if data.get("favorite_attempted"):
                self.save("favorite_started", data)
                raise Pending("favorite creation not confirmed; lookup again later")
            self.save("favorite_started", {"favorite_attempted": True})
            p = self.c["candidate"]
            await self.call(
                "/api.product.favorite/toggle",
                "POST",
                body={
                    "status": True,
                    "productInfo": {
                        "sku": self.sku,
                        "title": p.get("title", ""),
                        "coverImage": p.get("image", ""),
                        "price_info": {"sell_price": p["price"], "currency": "CNY"},
                    },
                },
            )
            favorites = await self.call(
                "/api.product.favorite/lists", query={"sku": self.sku, "page": 1, "page_size": 100}
            )
            rows = favorites if isinstance(favorites, list) else favorites.get("data", [])
            matches = [r for r in rows if str(r.get("sku")) == self.sku]
        if len(matches) != 1:
            raise ModuleError("source favorite not confirmed")
        data = {"source_key": self.sku, "favorite_id": int(matches[0]["id"])}
        self.save("draft_started", data)
        draft = await self.call("/api.product.favorite/edit_import", "POST", body={"id": data["favorite_id"]})
        draft_id = int(draft.get("jump_id") or draft.get("id") or 0)
        if draft_id <= 0:
            raise ModuleError("source draft acknowledgement missing")
        data["draft_id"] = draft_id
        self.save("draft_ready", data)
        detail = await self.call("/api.product.collect/detail", query={"id": draft_id, "is_online": 0})
        data |= {"detail": detail, "observed_at": time.time()}
        self.save("ready", data)
        return data


async def map_detail(snapshot, context, seller):
    """Convert the exact source variant, resolving IDs against the current official schema."""
    if snapshot.get("source_key") != context["candidate"]["source_key"]:
        raise ModuleError("source detail SKU mismatch")
    detail = snapshot["detail"]
    variants = detail.get("skus", [])
    if len(variants) != 1:
        raise ModuleError("source has multiple variants; precise variant selection required")
    variant = variants[0]
    category = detail.get("category_id", [])
    if len(category) != 3 or not all(str(x).isdigit() for x in category):
        raise ModuleError("source category incomplete")
    category_id, type_id = int(category[1]), int(category[2])
    schema = (
        await seller(
            "/v1/description-category/attribute",
            {"description_category_id": category_id, "type_id": type_id, "language": "DEFAULT"},
        )
    ).get("result", [])
    if not schema:
        raise ModuleError("official category schema unavailable")
    definitions = {int(a["id"]): a for a in schema}
    raw, provenance, issues = {"source_key": snapshot["source_key"]}, {}, []

    def put(key, value):
        if value is not None and value != "" and value != []:
            raw[key] = value
            provenance[key] = (
                f"source SKU {snapshot['source_key']}, ERP draft {snapshot['draft_id']}, observed {snapshot['observed_at']}"
            )

    put("description_category_id", category_id)
    put("type_id", type_id)
    put("name", variant.get("name") or detail.get("title"))
    put("images", variant.get("images"))
    # Price, stock, warehouse, tax and package dimensions remain OUR verified inputs.
    merged = {}
    for attribute in detail.get("common_attributes", []) + variant.get("attributes", []):
        aid = int(attribute["id"])
        value = attribute.get("values")
        if aid in merged and merged[aid] != value:
            issues.append(f"{aid}：原商品属性与变体属性冲突")
            continue
        merged[aid] = value
    if 85 not in merged and detail.get("brand_id"):
        merged[85] = [detail["brand_id"]]
    if 4191 in definitions and 4191 not in merged and detail.get("description"):
        merged[4191] = detail["description"]
    attributes = []
    for aid, values in merged.items():
        spec = definitions.get(aid)
        if not spec or spec.get("attribute_complex_id"):
            continue
        if values is None or values == "" or values == []:
            continue
        values = values if isinstance(values, list) else [values]
        normalized = []
        for value in values:
            if spec.get("dictionary_id"):
                identifier = (
                    value.get("dictionary_value_id", value.get("id")) if isinstance(value, dict) else value
                )
                if not str(identifier).isdigit() or int(identifier) <= 0:
                    issues.append(f"{aid}：缺少有效字典 ID")
                    continue
                identifier = int(identifier)
                result = await seller(
                    "/v1/description-category/attribute/values",
                    {
                        "description_category_id": category_id,
                        "type_id": type_id,
                        "attribute_id": aid,
                        "last_value_id": identifier - 1,
                        "limit": 100,
                        "language": "DEFAULT",
                    },
                )
                exact = [r for r in result.get("result", []) if r.get("id") == identifier]
                if len(exact) != 1:
                    # ERP may retain an obsolete dictionary ID. Recover only by exact text.
                    label = value.get("value") if isinstance(value, dict) else None
                    if aid == 85:
                        brand = detail.get("brand_select") or {}
                        if str(brand.get("id")) == str(identifier):
                            label = brand.get("value")
                    if isinstance(label, str) and label.strip():
                        found = await seller(
                            "/v1/description-category/attribute/values/search",
                            {
                                "description_category_id": category_id,
                                "type_id": type_id,
                                "attribute_id": aid,
                                "value": label,
                                "limit": 100,
                            },
                        )
                        exact = [
                            r
                            for r in found.get("result", [])
                            if str(r.get("value", "")).strip().casefold() == label.strip().casefold()
                        ]
                        if len(exact) == 1:
                            identifier = exact[0]["id"]
                if len(exact) != 1:
                    issues.append(f"{aid}：原字典值已失效或无法核验")
                    continue
                normalized.append({"dictionary_value_id": identifier, "value": exact[0]["value"]})
            elif isinstance(value, (str, int, float, bool)):
                normalized.append({"value": str(value).lower() if isinstance(value, bool) else str(value)})
            elif isinstance(value, dict) and isinstance(value.get("value"), str):
                normalized.append({"value": value["value"]})
        if normalized:
            attributes.append({"id": aid, "complex_id": 0, "values": normalized})
    put("attributes", attributes)
    raw["provenance"] = provenance
    required = [a["id"] for a in schema if a.get("is_required")]
    missing = sorted(set(required) - {a["id"] for a in attributes})
    return {
        "dossier": raw,
        "issues": issues,
        "required_missing": missing,
        "mapped_attributes": len(attributes),
        "source": "maozi-source-draft",
        "mapping_version": 2,
        "identity_review_required": [a["id"] for a in attributes if a["id"] in (85, 4389, 23487, 9048)],
    }
