"""Local dossier publisher. No ERP calls; ambiguous writes are reconciled, never replayed."""

import hashlib
import json
import math
import re
import time
import uuid

from .maozi import MaoziPublisher
from .modules import ModuleError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def assemble(context):
    """Copy traceable fields; never infer manufacturer, origin country, brand or tax."""
    candidate = context["candidate"]
    raw = dict(context.get("prepared", {}).get("source_dossier", {}))
    raw.update(candidate.get("origin", {}).get("ozon_dossier", {}))
    provenance = dict(raw.get("provenance", {}))
    store = context.get("store", {})
    configuration = store.get("config", {})
    if configuration.get("vat_client_id") == str(store.get("credentials", {}).get("client_id")):
        if "vat" in configuration:
            raw["vat"] = configuration["vat"]
            provenance["vat"] = configuration.get("vat_source", "store configuration")
    raw.setdefault("source_key", candidate["source_key"])
    evidence = context.get("match", {}).get("evidence", {})
    inputs = evidence.get("profit", {}).get("input", {})

    def put(key, value, source):
        if key not in raw and value is not None and value != "":
            raw[key] = value
            provenance[key] = source

    put("name", candidate.get("title"), "candidate.title")
    if candidate.get("image", "").startswith("https://"):
        put("images", [candidate["image"]], "candidate.image")
    if inputs.get("sell_price") == candidate.get("price"):
        put("currency_code", "CNY", "match.evidence.profit.input.sell_price (CNY contract)")
    for field, source in (
        ("depth", "package_length"),
        ("width", "package_width"),
        ("height", "package_height"),
        ("weight", "package_weight"),
    ):
        value = inputs.get(source)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        ):
            converted = value if field == "weight" else value * 10
            if float(converted).is_integer():
                put(field, int(converted), "match.evidence.profit.input." + source)
    if all(raw.get(k) for k in ("depth", "width", "height")):
        put("dimension_unit", "mm", "exact conversion from profit input cm")
    if raw.get("weight"):
        put("weight_unit", "g", "profit input weight in g")
    type_name = evidence.get("profit", {}).get("commission", {}).get("type_name_ru")
    if type_name:
        from .commissions import DATA

        exact = [
            (key, value)
            for key, value in DATA["taxonomy"].items()
            if value.get("name") == type_name and not value.get("disabled")
        ]
        if len(exact) == 1:
            type_id, definition = exact[0]
            put("type_id", int(type_id), "official taxonomy exact unique type name: " + type_name)
            put(
                "description_category_id", int(definition["category_id"]), "official taxonomy parent category"
            )
    raw["provenance"] = provenance
    return raw


def dossier(context):
    candidate = context["candidate"]
    raw = assemble(context)
    errors = []
    if raw.get("source_key") != candidate["source_key"]:
        errors.append("source_key：缺失或与候选不一致")
    fields = (
        "name",
        "description_category_id",
        "type_id",
        "currency_code",
        "vat",
        "depth",
        "width",
        "height",
        "dimension_unit",
        "weight",
        "weight_unit",
        "images",
        "attributes",
    )
    item = {k: raw[k] for k in fields if k in raw}
    for key in fields:
        if key not in item or item[key] in (None, "", []):
            errors.append(key + "：缺失")
    for key in ("description_category_id", "type_id", "depth", "width", "height", "weight"):
        if type(item.get(key)) is not int or item[key] <= 0:
            errors.append(key + "：需要正整数")
    if item.get("dimension_unit") != "mm" or item.get("weight_unit") != "g":
        errors.append("尺寸必须为 mm，重量必须为 g")
    if item.get("currency_code") != "CNY":
        errors.append("当前利润流程仅支持 CNY 定价")
    if not isinstance(item.get("name"), str) or not re.search(r"[А-Яа-яЁё]", item.get("name", "")):
        errors.append("name：需要俄文商品标题，不能直接使用英文采集标签")
    if not isinstance(item.get("vat"), str):
        errors.append("vat：必须提供账户适用的税率字符串")
    if not isinstance(item.get("images"), list) or not all(
        isinstance(u, str) and u.startswith("https://") for u in item.get("images", [])
    ):
        errors.append("images：需要 HTTPS 图片列表")
    provenance = raw.get("provenance", {})
    for key in fields:
        if not isinstance(provenance.get(key), str) or not provenance[key].strip():
            errors.append(key + "：缺少资料来源")
    price = candidate.get("price")
    if (
        not isinstance(price, (int, float))
        or isinstance(price, bool)
        or not math.isfinite(price)
        or price <= 0
    ):
        errors.append("price：无效")
    item["price"] = str(price)
    item["offer_id"] = context["idempotency_key"]
    return item, errors


class OzonDirectPublisher(MaoziPublisher):
    def __init__(self, context, database):
        from .store_vat import configured_vat

        profile = configured_vat(database, context["store"]["credentials"]["client_id"])
        if profile:
            store = context["store"]
            context = context | {
                "store": store
                | {
                    "config": store["config"]
                    | {
                        "vat": profile["vat"],
                        "vat_client_id": profile["client_id"],
                        "vat_source": profile["source"],
                    }
                }
            }
        super().__init__(context)
        self.db = database

    async def seller(self, path, body):
        if path != "/v2/products/stocks":
            return await super().seller(path, body)
        # Preserve the acknowledgement so a definitive rejection can be distinguished
        # from an unknown network outcome. Receipts are private and encrypted.
        receipt = uuid.uuid4().hex
        with self.db.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS direct_stock_receipts(id TEXT PRIMARY KEY,offer_id TEXT NOT NULL,at REAL NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO direct_stock_receipts VALUES(?,?,?,?,?)",
                (receipt, self.c["idempotency_key"], time.time(), "started", self.db.seal({"request": body})),
            )
        try:
            result = await super().seller(path, body)
        except BaseException as error:
            with self.db.connect() as db:
                db.execute(
                    "UPDATE direct_stock_receipts SET state='unknown',body=? WHERE id=?",
                    (self.db.seal({"request": body, "error_type": type(error).__name__}), receipt),
                )
            raise
        with self.db.connect() as db:
            db.execute(
                "UPDATE direct_stock_receipts SET state='responded',body=? WHERE id=?",
                (self.db.seal({"request": body, "response": result}), receipt),
            )
        return result

    async def erp(self, *args, **kwargs):
        raise ModuleError("direct publisher must not call ERP")

    async def identity(self):
        data = await self.seller("/v2/warehouse/list", {})
        matches = [
            w
            for w in data.get("warehouses", [])
            if str(w.get("warehouse_id")) == str(self.config["warehouse_id"])
            and w.get("status") in ("active", "working", "created")
        ]
        if len(matches) != 1:
            raise ModuleError("warehouse mismatch")
        return {"verified": True}

    def binding(self):
        return {
            "owner": self.c["owner"],
            "store": self.store["id"],
            "client": str(self.keys["client_id"]),
            "warehouse": str(self.config["warehouse_id"]),
            "source": self.c["candidate"]["source_key"],
            "offer": self.c["idempotency_key"],
        }

    async def prepare(self):
        source_result = None
        supplied = self.c["candidate"].get("origin", {}).get("ozon_dossier", {})
        if not supplied.get("attributes") and self.keys.get("erp_token"):
            from .source_detail import SourceCollector, map_detail

            collector = SourceCollector(self.db, self.c)
            snapshot = await collector.collect()
            cached = snapshot.get("mapping")
            if (
                cached
                and cached.get("mapping_version") == 3
                and snapshot.get("mapping_client") == str(self.keys["client_id"])
            ):
                source_result = cached
            else:
                source_result = await map_detail(snapshot, self.c, self.seller)
                collector.save(
                    "ready",
                    snapshot | {"mapping": source_result, "mapping_client": str(self.keys["client_id"])},
                )
            self.c = self.c | {"prepared": {"source_dossier": source_result["dossier"]}}
        item, errors = dossier(self.c)
        if source_result:
            errors.extend(source_result["issues"])
        if any(type(item.get(k)) is not int or item[k] <= 0 for k in ("description_category_id", "type_id")):
            return {"ready": False, "needs_input": True, "missing": errors}
        await self.identity()
        if await self.product():
            raise ModuleError("offer already exists; refusing overwrite")
        schema = await self.seller(
            "/v1/description-category/attribute",
            {
                "description_category_id": item["description_category_id"],
                "type_id": item["type_id"],
                "language": "DEFAULT",
            },
        )
        definitions = schema.get("result")
        if not isinstance(definitions, list) or not definitions:
            raise ModuleError("invalid category schema")
        attrs = item.get("attributes", [])
        if not isinstance(attrs, list):
            raise ModuleError("invalid attributes")
        supplied = {}
        for a in attrs:
            if not isinstance(a, dict) or type(a.get("id")) is not int or a["id"] in supplied:
                raise ModuleError("invalid or duplicate attribute")
            supplied[a["id"]] = a
        known = {a["id"]: a for a in definitions}
        for aid, a in known.items():
            if a.get("is_required") and not supplied.get(aid, {}).get("values"):
                errors.append(f"{aid} {a['name']}：必填属性缺失")
        for aid, a in supplied.items():
            spec = known.get(aid)
            if not spec or spec.get("attribute_complex_id") or a.get("complex_id", 0):
                errors.append(f"{aid}：未知属性或暂不支持的复合属性")
                continue
            values = a.get("values")
            if not isinstance(values, list) or not values:
                errors.append(f"{aid}：属性值为空")
                continue
            maximum = spec.get("max_value_count") or (100 if spec.get("is_collection") else 1)
            if len(values) > maximum:
                errors.append(f"{aid}：属性值过多")
            for v in values:
                if not isinstance(v, dict) or not str(v.get("value", "")).strip():
                    errors.append(f"{aid}：属性值无效")
                    continue
                if spec.get("dictionary_id"):
                    result = await self.seller(
                        "/v1/description-category/attribute/values/search",
                        {
                            "description_category_id": item["description_category_id"],
                            "type_id": item["type_id"],
                            "attribute_id": aid,
                            "value": v["value"],
                            "limit": 100,
                        },
                    )
                    if not any(
                        x.get("id") == v.get("dictionary_value_id") and x.get("value") == v["value"]
                        for x in result.get("result", [])
                    ):
                        errors.append(f"{aid}：字典值未获官方接口核实")
        if errors:
            return {"ready": False, "needs_input": True, "missing": errors}
        frozen = {"binding": self.binding(), "item": item, "checked_at": time.time()}
        return {
            "ready": True,
            "frozen": frozen,
            "digest": digest(frozen),
            "source_dossier": assemble(self.c),
            "source_mapping": {k: v for k, v in (source_result or {}).items() if k != "dossier"},
        }

    async def invoke(self, op):
        if op == "prepare":
            return await self.prepare()
        if op == "publish":
            prepared = self.c["prepared"]
            frozen = prepared.get("frozen", {})
            if digest(frozen) != prepared.get("digest") or frozen.get("binding") != self.binding():
                raise ModuleError("frozen dossier identity mismatch")
            item, errors = dossier(self.c)
            if errors or item != frozen.get("item") or time.time() - frozen["checked_at"] > 21600:
                raise ModuleError("dossier changed or expired")
            await self.identity()
            if await self.product():
                raise ModuleError("offer already exists; refusing overwrite")
            if (await super().invoke("quota"))["remaining"] <= 0:
                return {"not_sent": True}
            # Unique durable marker BEFORE the external call, shared across process restarts.
            with self.db.connect() as db:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO ozon_direct_writes VALUES(?,?,?,?,?,?)",
                    (
                        self.c["idempotency_key"],
                        self.c["owner"],
                        digest(self.binding()),
                        prepared["digest"],
                        None,
                        time.time(),
                    ),
                )
                inserted = cursor.rowcount == 1
            if not inserted:
                return {"accepted": False, "unknown": True}
            response = await self.seller("/v3/product/import", {"items": [item]})
            task = (response.get("result") or {}).get("task_id")
            if not task:
                raise ModuleError("import acknowledgement missing; reconcile only")
            with self.db.connect() as db:
                db.execute(
                    "UPDATE ozon_direct_writes SET task_id=? WHERE offer_id=?",
                    (str(task), self.c["idempotency_key"]),
                )
            return {"accepted": True, "task_id": str(task)}
        if op == "reconcile":
            with self.db.connect() as db:
                row = db.execute(
                    "SELECT * FROM ozon_direct_writes WHERE offer_id=?", (self.c["idempotency_key"],)
                ).fetchone()
            if row and row["binding_hash"] != digest(self.binding()):
                raise ModuleError("publication binding mismatch")
            if row and row["task_id"]:
                result = await self.seller("/v1/product/import/info", {"task_id": int(row["task_id"])})
                items = (result.get("result") or {}).get("items", [])
                if any(p.get("offer_id") != self.c["idempotency_key"] for p in items):
                    raise ModuleError("import task identity mismatch")
                if any(p.get("errors") or p.get("status") == "failed" for p in items):
                    from .title_recovery import recover_title

                    return await recover_title(self, row, [e for p in items for e in p.get("errors", [])])
            product = await self.product()
            if row and product and product.get("errors"):
                from .title_recovery import recover_title

                return await recover_title(self, row, product["errors"])
            return {
                "found": bool(product and product.get("sku")),
                "product_id": str(product["id"]) if product else "",
                "issue": bool(product and (product.get("errors") or product.get("is_archived"))),
                "store_id": self.store["id"],
            }
        if op not in ("identity", "quota", "stock", "check_stock"):
            raise ModuleError("unknown operation")
        return await super().invoke(op)
