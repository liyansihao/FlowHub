"""Versioned module boundary. Modules cannot select another user's store or database."""

import asyncio
import importlib
import ipaddress
import json
import math
import socket
import time
from urllib.parse import urlparse

import httpx


class ModuleError(Exception):
    pass


class Pending(ModuleError):
    pass


def number(value, low=0, high=1e9):
    if isinstance(value, bool):
        raise ModuleError("invalid number")
    v = float(value)
    if not math.isfinite(v) or not low <= v <= high:
        raise ModuleError("invalid number")
    return v


def image_url(value):
    if not value:
        return ""
    p = urlparse(str(value))
    if p.scheme == "http" and p.hostname and p.hostname.endswith(".alicdn.com"):
        value = "https://" + str(value)[7:]
        p = urlparse(value)
    if p.scheme == "https" and p.hostname:
        return str(value)[:2000]
    if str(value).startswith("/assets/") and ".." not in str(value):
        return str(value)
    raise ModuleError("invalid image URL")


def candidate(row):
    key = str(row["source_key"])
    if not 1 <= len(key) <= 150:
        raise ModuleError("invalid source key")
    return dict(
        source_key=key,
        title=str(row["title"])[:250],
        image=image_url(row.get("image", "")),
        price=number(row["price"], 0.01),
        weight_g=number(row["weight_g"], 0),
        dimensions_cm=[number(x, 0, 500) for x in row["dimensions_cm"]],
        category_id=str(row.get("category_id", "")),
        pure_fbs=row.get("pure_fbs") is True,
        origin=row.get("origin", {}),
    )


async def public_endpoint(url):
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise ModuleError("HTTPS module endpoint required")
    addresses = await asyncio.to_thread(
        socket.getaddrinfo, p.hostname, p.port or 443, type=socket.SOCK_STREAM
    )
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ModuleError("private network endpoint prohibited")


class ModuleHost:
    def __init__(self, db):
        self.db = db

    async def invoke(self, module, operation, context, secret=""):
        if module["driver"] == "source-library":
            from .source_library import candidate_page

            if operation != "candidates":
                raise ModuleError("source library only supplies candidates")
            return candidate_page(self.db, context)
        if module["driver"] == "comparebot":
            from .comparebot import invoke

            return await invoke(operation, context, secret)
        if module["driver"] == "local-profit":
            from .local_profit import invoke

            return await invoke(module["endpoint"], operation, context)
        if module["driver"] == "flowb":
            from .compat import invoke

            return await invoke(operation, context, secret)
        if module["driver"] == "demo":
            return await self.demo(operation, context)
        if module["driver"] == "ozon-direct":
            from .ozon_direct import OzonDirectPublisher

            return await OzonDirectPublisher(context, self.db).invoke(operation)
        if module["driver"] == "maozi":
            from .maozi import MaoziPublisher

            return await MaoziPublisher(context).invoke(operation)
        if module["driver"] == "plugin":
            # Server-installed allowlist only; ordinary users cannot upload/import Python.
            manifest = self.db.directory.parent / "plugins/installed.json"
            entries = json.loads(manifest.read_text()) if manifest.exists() else {}
            entry = entries.get(module["endpoint"])
            if not entry or not entry.startswith("flowhub_plugins."):
                raise ModuleError("plugin not installed")
            plugin = importlib.import_module(entry)
            return await asyncio.wait_for(plugin.invoke(operation, context, secret), 30)
        await public_endpoint(module["endpoint"])
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "POST",
                module["endpoint"],
                headers={
                    "Authorization": "Bearer " + secret,
                    "Idempotency-Key": context.get("idempotency_key", ""),
                },
                json={"version": "1", "operation": operation, "context": context},
            ) as r:
                r.raise_for_status()
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise ModuleError("oversized response")
                payload = json.loads(body)
            if payload.get("version") != "1" or not isinstance(payload.get("result"), dict):
                raise ModuleError("invalid protocol")
            return payload["result"]

    async def demo(self, op, c):
        await asyncio.sleep(0.02)
        if op == "candidates":
            n = int(c.get("cursor") or 0)
            names = ["桌面收纳架", "旅行收纳袋", "便携挂钩", "文件收纳夹", "厨房置物架", "硅胶隔热垫"]
            return {
                "items": [
                    dict(
                        source_key=f"demo-{i}",
                        title=names[i % 6],
                        image=f"/assets/product-{i % 6}.svg",
                        price=39 + i % 7,
                        weight_g=180,
                        dimensions_cm=[20, 15, 5],
                        pure_fbs=True,
                    )
                    for i in range(n, n + 3)
                ],
                "cursor": str(n + 3),
            }
        if op == "match":
            p = c["candidate"]
            idx = int(p["source_key"].split("-")[-1])
            return dict(
                supplier_id=f"demo-supplier-{idx}",
                supplier_url="https://www.1688.com/",
                image=p["image"],
                purchase=8 + idx % 4,
                score=0.88 if idx % 7 else 0.61,
                dhash=0.8,
                observed_at=time.time(),
            )
        if op == "profit":
            p = c["candidate"]
            m = c["match"]
            cost = m["purchase"] + 8 + p["price"] * 0.15
            return dict(
                cost_return=(p["price"] - cost) / cost * 100,
                total_cost=cost,
                logistics=c["rules"]["logistics"],
                route_available=True,
                observed_at=time.time(),
            )
        if op == "quota":
            store = c["store"]
            now = time.time()
            start = now - now % 300
            with self.db.connect() as db:
                used = db.execute(
                    "SELECT COUNT(*) FROM demo_effects WHERE owner=? AND id IN (SELECT id FROM jobs WHERE store_id=? AND created>=?)",
                    (c["owner"], store["id"], start),
                ).fetchone()[0]
            return dict(remaining=max(0, 12 - used), reset_at=start + 300, store_id=store["id"])
        if op == "prepare":
            return {"ready": True}
        if op == "publish":
            with self.db.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO demo_effects(id,owner) VALUES(?,?)",
                    (c["idempotency_key"], c["owner"]),
                )
            return {"accepted": True}
        if op == "reconcile":
            with self.db.connect() as db:
                r = db.execute(
                    "SELECT * FROM demo_effects WHERE id=? AND owner=?", (c["idempotency_key"], c["owner"])
                ).fetchone()
            return dict(
                found=bool(r), product_id=c["idempotency_key"], issue=False, store_id=c["store"]["id"]
            )
        if op == "stock":
            with self.db.connect() as db:
                db.execute(
                    "UPDATE demo_effects SET stock=? WHERE id=? AND owner=?",
                    (c["rules"]["stock"], c["idempotency_key"], c["owner"]),
                )
            return {"accepted": True}
        if op == "check_stock":
            with self.db.connect() as db:
                r = db.execute(
                    "SELECT stock FROM demo_effects WHERE id=? AND owner=?",
                    (c["idempotency_key"], c["owner"]),
                ).fetchone()
            return dict(
                selling=bool(r),
                stock=r["stock"] if r else 0,
                store_id=c["store"]["id"],
                warehouse_id=c["store"]["config"]["warehouse_id"],
            )
        raise ModuleError("unsupported operation")
