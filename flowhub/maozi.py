"""Existing FlowEF Maozi protocol, parameterized with this user's store credentials only."""

from datetime import datetime

import httpx

from .modules import ModuleError, Pending


class MaoziPublisher:
    def __init__(self, context):
        self.c = context
        self.store = context["store"]
        self.config = self.store["config"]
        self.keys = self.store["credentials"]

    async def erp(self, method, path, body=None, params=None):
        if self.c.get('acquisition_gateway'):
            from .source_gateway import request
            result,_=await request(self.keys,path,method,params,body,proxy=self.config.get("erp_proxy"))
            return result
        async with httpx.AsyncClient(
            base_url="https://api.maozierp.com",
            headers={
                "Authorization": "Bearer " + self.keys["erp_token"],
                "Client": "pc",
                "Accept": "application/json",
                "Accept-Language": "zh-CN",
            },
            timeout=25,
            trust_env=False,
            proxy=self.config.get('erp_proxy'),
        ) as client:
            r = await client.request(method, path, json=body, params=params)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 1:
                raise ModuleError("ERP request failed")
            return data["data"]

    async def seller(self, path, body):
        async with httpx.AsyncClient(
            base_url="https://api-seller.ozon.ru",
            headers={"Client-Id": self.keys["client_id"], "Api-Key": self.keys["api_key"]},
            timeout=25,
            trust_env=False,
        ) as client:
            r = await client.post(path, json=body)
            r.raise_for_status()
            return r.json()

    async def rows(self, path, params):
        result = []
        for page in range(1, 21):
            data = await self.erp("GET", path, params=params | {"page": page, "page_size": 100})
            rows = data if isinstance(data, list) else data.get("data", [])
            if not isinstance(rows, list):
                raise ModuleError("invalid rows")
            result.extend(rows)
            if isinstance(data, dict) and page >= int(data.get("last_page", 20)):
                return result
            if len(rows) < 100:
                return result
        raise ModuleError("truncated lookup")

    async def identity(self):
        rows = await self.rows("/api.shop/lists", {"scene": "erp"})
        matches = [r for r in rows if str(r["id"]) == str(self.config["shop_id"])]
        if len(matches) != 1:
            raise ModuleError("ERP store mismatch")
        row = matches[0]
        if (
            row.get("status") not in (1, "1")
            or str(row.get("api_client_id")) != str(self.keys["client_id"])
            or str(row.get("watermark_id")) != str(self.config["watermark_id"])
        ):
            raise ModuleError("ERP identity mismatch")
        data = await self.seller("/v2/warehouse/list", {})
        if not any(
            str(w["warehouse_id"]) == str(self.config["warehouse_id"])
            and w.get("status") in ("active", "working", "created")
            for w in data.get("warehouses", [])
        ):
            raise ModuleError("warehouse mismatch")
        return {"verified": True}

    async def product(self):
        data = await self.seller("/v3/product/info/list", {"offer_id": [self.c["idempotency_key"]]})
        rows = data.get("items", [])
        if len(rows) > 1 or any(p["offer_id"] != self.c["idempotency_key"] for p in rows):
            raise ModuleError("product identity mismatch")
        return rows[0] if rows else None

    async def invoke(self, op):
        c = self.c
        offer = c.get("idempotency_key")
        sku = c.get("candidate", {}).get("source_key")
        if op == "identity":
            return await self.identity()
        if op == "quota":
            d = (await self.seller("/v4/product/info/limit", {}))["daily_create"]
            return dict(
                remaining=max(0, int(d["limit"]) - int(d["usage"])),
                reset_at=datetime.fromisoformat(d["reset_at"].replace("Z", "+00:00")).timestamp(),
                store_id=self.store["id"],
            )
        if op == "prepare":
            await self.identity()
            rows = await self.rows("/api.product.favorite/lists", {"sku": sku})
            exact = [r for r in rows if str(r.get("sku")) == sku]
            if exact and exact[0].get("is_imported") in (1, "1", True):
                raise ModuleError("already imported")
            if not exact:
                p = c["candidate"]
                await self.erp(
                    "POST",
                    "/api.product.favorite/toggle",
                    {
                        "productInfo": {
                            "sku": sku,
                            "coverImage": p["image"],
                            "price_info": {"sell_price": p["price"], "currency": "CNY"},
                            "title": p["title"],
                        },
                        "status": True,
                    },
                )
                return {"ready": False}
            return {"ready": True, "favorite_id": str(exact[0]["id"])}
        if op == "publish":
            await self.identity()
            q = await self.invoke("quota")
            if q["remaining"] <= 0:
                return {"not_sent": True}
            p = c["candidate"]
            price = p["price"]
            await self.erp(
                "POST",
                "/api.selection.follow/import",
                {
                    "scene": "erp",
                    "shop_ids": [int(self.config["shop_id"])],
                    "brand": "none",
                    "image_order": "none",
                    "watermark_id": int(self.config["watermark_id"]),
                    "floating_price": None,
                    "rows": [
                        {
                            "id": int(c["prepared"]["favorite_id"]),
                            "sku": sku,
                            "title": p["title"],
                            "cover_image": p["image"],
                            "link": f"https://www.ozon.ru/product/{sku}/",
                            "sell_price": price,
                            "price": price,
                            "old_price": round(price * 2, 2),
                            "offer_id": offer,
                            "brand": "",
                            "source": "favorite",
                            "source_currency": "CNY",
                        }
                    ],
                },
            )
            return {"accepted": True}
        p = await self.product()
        if op == "reconcile":
            return dict(
                found=bool(p and p.get("sku")),
                product_id=str(p["id"]) if p else "",
                issue=bool(p and (p.get("errors") or p.get("is_archived"))),
                store_id=self.store["id"],
            )
        if not p or not p.get("sku"):
            raise Pending("product pending")
        if p.get("errors") or p.get("is_archived"):
            raise ModuleError("product blocked")
        if op == "stock":
            await self.identity()
            d = await self.seller(
                "/v2/products/stocks",
                {
                    "stocks": [
                        {
                            "offer_id": offer,
                            "product_id": p["id"],
                            "warehouse_id": int(self.config["warehouse_id"]),
                            "stock": c["rules"]["stock"],
                        }
                    ]
                },
            )
            rows = d.get("result", [])
            if len(rows) != 1 or rows[0].get("updated") is not True or rows[0].get("errors"):
                raise ModuleError("stock not acknowledged")
            return {"accepted": True}
        if op == "check_stock":
            d = await self.seller(
                "/v2/product/info/stocks-by-warehouse/fbs", {"sku": [p["sku"]], "limit": 1000}
            )
            if d.get("has_next"):
                raise ModuleError("truncated stock result")
            rows = [
                r
                for r in d.get("products", [])
                if str(r.get("warehouse_id")) == str(self.config["warehouse_id"])
                and str(r.get("product_id")) == str(p["id"])
                and r.get("offer_id") == offer
            ]
            if len(rows) > 1:
                raise ModuleError("ambiguous stock")
            selling = (p.get("statuses") or {}).get("status_name", "").casefold() in (
                "selling",
                "продается",
                "продаётся",
            )
            return dict(
                selling=selling,
                stock=rows[0]["present"] if rows else 0,
                store_id=self.store["id"],
                warehouse_id=str(self.config["warehouse_id"]),
            )
        raise ModuleError("unknown operation")
