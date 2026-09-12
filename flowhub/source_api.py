"""Source browsing and collection are independent of listing workflow activation."""

import asyncio
import json
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .source_acquisition import SourceAcquirer, erp_request
from .source_history import import_history
from .source_library import SourceFilters, SourceLibrary
from .storefront import StorefrontCollector, StorefrontError


def register_source_api(app, db, scope, admin):
    library = SourceLibrary(db)

    storefront = StorefrontCollector(library)

    class BrowserPacket(BaseModel):
        model_config = ConfigDict(extra="forbid")
        html: str = Field(max_length=6_000_000)
        requested_url: str = Field(max_length=10000)

    @app.get("/api/sources/storefront")
    def storefront_tasks(owner=Depends(scope)):
        return storefront.tasks(owner)

    @app.post("/api/sources/storefront/prepare")
    def storefront_prepare(owner=Depends(scope)):
        manifest = {}
        with db.connect() as c:
            for r in c.execute(
                "SELECT sku,shop,offer,body FROM sourcing_seeds WHERE owner=? AND archived=0", (owner,)
            ):
                seller = str(json.loads(r["body"]).get("seller_id") or "")
                if seller.isdigit() and int(seller) > 0:
                    manifest.setdefault(seller, []).append(
                        dict(sku=r["sku"], shop=r["shop"], offer=r["offer"])
                    )
        return storefront.prepare(owner, manifest)

    @app.post("/api/sources/storefront/{seller}/import")
    def storefront_import(seller: str, payload: BrowserPacket, owner=Depends(scope)):
        try:
            return storefront.ingest(owner, seller, payload.html, payload.requested_url)
        except KeyError:
            raise HTTPException(404, "来源店铺任务不存在") from None
        except StorefrontError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/api/sources/storefront/{seller}/attempts")
    def storefront_attempts(seller: str, owner=Depends(scope), cursor: int = Query(default=0, ge=0)):
        with db.connect() as c:
            return [
                dict(r) | {"body": json.loads(r["body"])}
                for r in c.execute(
                    "SELECT * FROM storefront_attempts WHERE owner=? AND seller=? AND id>? ORDER BY id LIMIT 50",
                    (owner, seller, cursor),
                )
            ]

    @app.post("/api/sources/storefront/{seller}/{action}")
    def storefront_control(seller: str, action: str, owner=Depends(scope)):
        try:
            return storefront.control(owner, seller, action)
        except KeyError:
            raise HTTPException(404, "来源店铺任务不存在") from None
        except StorefrontError as error:
            raise HTTPException(409, str(error)) from None

    class Settings(BaseModel):
        model_config = ConfigDict(extra="forbid")
        enabled: bool = False
        filters: dict = Field(default_factory=dict)
        erp_token: str | None = Field(default=None, max_length=8000)

    @app.get("/api/sources")
    def sources(
        owner=Depends(scope),
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
        state: str | None = None,
    ):
        if state not in (None, "qualified", "needs_review", "rejected"):
            raise HTTPException(400, "未知筛选状态")
        with db.connect() as c:
            settings = c.execute("SELECT body FROM sourcing_settings WHERE owner=?", (owner,)).fetchone()
        filters = SourceFilters(**json.loads(settings[0])) if settings else SourceFilters()
        return library.query(owner, filters, cursor, limit, state) | {"status": library.status(owner)}

    @app.put("/api/sources/settings")
    def settings(payload: Settings, owner=Depends(scope)):
        try:
            SourceFilters(**payload.filters)
        except (TypeError, ValueError):
            raise HTTPException(400, "筛选条件无效") from None
        with db.connect() as c:
            saved = c.execute("SELECT secret,body FROM sourcing_settings WHERE owner=?", (owner,)).fetchone()
            token = (
                payload.erp_token
                if payload.erp_token is not None
                else (db.open(saved[0]).get("erp_token", "") if saved else "")
            )
            if payload.enabled and not token:
                raise HTTPException(400, "请先配置商品来源的毛子 ERP 连接")
            c.execute(
                "INSERT OR REPLACE INTO sourcing_settings VALUES(?,?,?,?)",
                (owner, payload.enabled, json.dumps(payload.filters), db.seal({"erp_token": token})),
            )
            if not saved or json.loads(saved[1]) != payload.filters:
                c.execute(
                    "DELETE FROM sourcing_yields WHERE task IN (SELECT id FROM sourcing_tasks WHERE owner=?)",
                    (owner,),
                )
                c.execute(
                    "DELETE FROM sourcing_pages WHERE task IN (SELECT id FROM sourcing_tasks WHERE owner=? AND kind IN ('category','keyword','seller_scan'))",
                    (owner,),
                )
                c.execute(
                    "UPDATE sourcing_tasks SET page=1,due=0,lease=NULL,lease_until=0 WHERE owner=? AND kind IN ('category','keyword','seller_scan')",
                    (owner,),
                )
            source_workflow = c.execute(
                "SELECT 1 FROM workflows w JOIN modules m ON m.id=json_extract(w.modules,'$.candidates') WHERE w.owner=? AND m.driver='source-library'",
                (owner,),
            ).fetchone()
            if source_workflow:
                c.execute("DELETE FROM candidate_pages WHERE owner=?", (owner,))
                c.execute("UPDATE workflows SET cursor='' WHERE owner=?", (owner,))
            if payload.erp_token:
                c.execute(
                    "UPDATE sourcing_tasks SET state='ready',due=0,error=NULL WHERE owner=? AND error='authentication'",
                    (owner,),
                )
        return library.status(owner)

    @app.post("/api/sources/import-history")
    async def history(owner=Depends(scope), _=Depends(admin)):
        root = Path(os.environ.get("FLOWHUB_LEGACY_ROOT", Path(__file__).resolve().parents[2]))
        return await asyncio.to_thread(import_history, library, owner, root)

    @app.get("/api/sources/seeds")
    def seeds(owner=Depends(scope), offset: int = Query(default=0, ge=0)):
        with db.connect() as c:
            return [
                dict(r) | {"body": json.loads(r["body"])}
                for r in c.execute(
                    "SELECT shop,offer,sku,archived,sales,checked,body FROM sourcing_seeds WHERE owner=? ORDER BY (sales>0) DESC,sales DESC,shop,offer LIMIT 100 OFFSET ?",
                    (owner, offset),
                )
            ]

    @app.post("/api/sources/cycle")
    async def cycle(owner=Depends(scope)):
        with db.connect() as c:
            settings = c.execute("SELECT * FROM sourcing_settings WHERE owner=?", (owner,)).fetchone()
        if not settings or not settings["enabled"]:
            raise HTTPException(409, "请先启用商品来源采集")
        return await SourceAcquirer(library).cycle(owner, db.open(settings["secret"])["erp_token"])

    @app.get("/api/sources/tasks")
    def tasks(owner=Depends(scope), offset: int = Query(default=0, ge=0)):
        with db.connect() as c:
            return [
                dict(r) | {"body": json.loads(r["body"])}
                for r in c.execute(
                    "SELECT id,kind,body,page,state,error,failures,successes,last_at FROM sourcing_tasks WHERE owner=? ORDER BY id LIMIT 100 OFFSET ?",
                    (owner, offset),
                )
            ]

    @app.post("/api/sources/tasks/{task_id}/{action}")
    def control_task(task_id: str, action: str, owner=Depends(scope)):
        try:
            row = library.control_task(owner, task_id, action)
            return {k: row[k] for k in ("id", "page", "state", "error", "failures")}
        except KeyError:
            raise HTTPException(404, "采集任务不存在") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/api/sources/tasks/{task_id}/attempts")
    def attempts(task_id: str, owner=Depends(scope), cursor: int = Query(default=0, ge=0)):
        with db.connect() as c:
            return [
                dict(r) | {"body": json.loads(r["body"])}
                for r in c.execute(
                    "SELECT id,page,at,state,body FROM sourcing_attempts WHERE owner=? AND task=? AND id>? ORDER BY id LIMIT 50",
                    (owner, task_id, cursor),
                )
            ]

    @app.post("/api/sources/{sku}/recheck")
    async def recheck(sku: str, seller: str = "", owner=Depends(scope)):
        import re
        import time

        with db.connect() as c:
            row = c.execute(
                "SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?",
                (owner, sku, seller),
            ).fetchone()
            settings = c.execute("SELECT secret FROM sourcing_settings WHERE owner=?", (owner,)).fetchone()
        if not row:
            raise HTTPException(404, "来源商品不存在")
        if not settings:
            raise HTTPException(409, "请先配置商品来源连接")
        try:
            data = await erp_request("/api.chrome/sku3", {"sku": sku}, db.open(settings[0])["erp_token"])
            detail = data.get("data")
            if not isinstance(detail, dict) or str(detail.get("sku")) != sku:
                raise ValueError("identity mismatch")
        except Exception:
            raise HTTPException(502, "实时核查未完成，已有资料已保留") from None
        product = json.loads(row[0])
        weight = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)g", str(detail.get("custom_weight") or ""))
        dimensions = re.fullmatch(
            r"([0-9.]+)\s*x\s*([0-9.]+)\s*x\s*([0-9.]+)mm", str(detail.get("custom_volume") or "")
        )
        observed = time.time()
        product["live_check"] = {
            "observed_at": observed,
            "source": "maozi:/api.chrome/sku3",
            "sku": sku,
            "sales_schema": detail.get("salesSchema"),
            "weight_g": float(weight[1]) if weight else None,
            "dimensions_cm": [float(dimensions[i]) / 10 for i in (1, 2, 3)] if dimensions else None,
            "current_price_rub": None,
            "specifications": None,
            "missing": ["current_price_rub", "specifications"],
            "ready_for_pricing": False,
        }
        product["live_check"]["missing"] = [
            field
            for field in ("sales_schema", "weight_g", "dimensions_cm", "current_price_rub", "specifications")
            if product["live_check"].get(field) is None
        ]
        product["live_check"]["raw"] = detail
        product["verified_at"] = observed
        # Keep collected_at: rechecking logistics does not refresh ranking statistics.
        library.put(owner, product, {"channel": "maozi-live-check", "observed_at": observed})
        return product["live_check"]

    @app.get("/api/sources/export")
    def export(owner=Depends(scope), cursor: int = Query(default=0, ge=0)):
        with db.connect() as c:
            settings = c.execute("SELECT body FROM sourcing_settings WHERE owner=?", (owner,)).fetchone()
        filters = SourceFilters(**json.loads(settings[0])) if settings else SourceFilters()
        page = library.query(owner, filters, cursor=cursor, state="qualified")
        return {
            "contract": "flowhub-source-candidates-v1",
            "items": [
                {
                    "source_key": p["sku"],
                    "seller_id": p["seller_id"],
                    "title": p["title"],
                    "image": p["image"],
                    "url": p["url"],
                    "category_id": p["category_id"],
                    "average_price_rub": p["average_price_rub"],
                    "current_price_rub": (p.get("live_check") or {}).get("current_price_rub"),
                    "weight_g": p["weight_g"],
                    "specifications": p.get("specifications"),
                    "evidence_hash": p["evidence_hash"],
                    "provenance": p["provenance"],
                    "assessment": p["assessment"],
                    "live_check": p.get("live_check"),
                    "ready_for_pricing": False,
                    "supplier_match": {
                        "status": "pending",
                        "url": None,
                        "spec_match": None,
                        "purchase_price": None,
                    },
                }
                for p in page["items"]
            ],
            "cursor": page["cursor"],
            "has_more": page["has_more"],
        }

    @app.post("/api/sources/{sku}/handoff")
    async def handoff(sku: str, seller: str = "", owner=Depends(scope)):
        import secrets
        import time

        from .modules import image_url
        from .source_delists import read_delists
        from .source_library import assess

        try:
            exclusion = await read_delists()
        except Exception:
            raise HTTPException(503, "飞书明确下架清单暂不可用，候选已保留") from None
        if sku in exclusion["skus"]:
            raise HTTPException(409, "商品在明确下架清单中")
        with db.connect() as c:
            row = c.execute(
                "SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?",
                (owner, sku, seller),
            ).fetchone()
            settings = c.execute("SELECT body FROM sourcing_settings WHERE owner=?", (owner,)).fetchone()
            workflow = c.execute("SELECT * FROM workflows WHERE owner=?", (owner,)).fetchone()
            blocked = c.execute(
                "SELECT 1 FROM blocks WHERE owner=? AND source_key=?", (owner, sku)
            ).fetchone()
            if not row or not workflow:
                raise HTTPException(404, "商品或主流程不存在")
            if blocked:
                raise HTTPException(409, "商品在禁止清单中")
            if c.execute("SELECT 1 FROM sourcing_seeds WHERE owner=? AND sku=?", (owner, sku)).fetchone():
                raise HTTPException(409, "商品已有历史上架记录，不作为新增候选")
            product = json.loads(row[0])
            filters = SourceFilters(**json.loads(settings[0])) if settings else SourceFilters()
            if assess(product, filters)["state"] != "qualified":
                raise HTTPException(409, "商品尚未通过当前初筛条件")
            key = "fh-source-" + secrets.token_hex(12)
            now = time.time()
            candidate = {
                "source_key": sku,
                "title": product["title"],
                "image": image_url(product["image"]),
                "price": None,
                "average_price_rub": product["average_price_rub"],
                "weight_g": product["weight_g"],
                "dimensions_cm": product.get("dimensions_cm"),
                "category_id": product["category_id"],
                "origin": product,
                "source_contract": "flowhub-source-candidates-v1",
            }
            data = {
                "candidate": candidate,
                "rules": json.loads(workflow["rules"]),
                "dossier_missing": ["current_price_rub", "specifications", "supplier_match"],
                "source_evidence_hash": product["evidence_hash"],
            }
            c.execute(
                "INSERT OR IGNORE INTO jobs(id,owner,source_key,phase,data,modules,next_at,created,updated,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    owner,
                    sku,
                    "attention",
                    json.dumps(data),
                    workflow["modules"],
                    now,
                    now,
                    now,
                    "来源初筛通过；当前售价、规格及1688同款待核查",
                ),
            )
            job = c.execute(
                "SELECT id,phase FROM jobs WHERE owner=? AND source_key=?", (owner, sku)
            ).fetchone()
        return {"job_id": job["id"], "phase": job["phase"], "created": job["id"] == key, "submitted": False}
