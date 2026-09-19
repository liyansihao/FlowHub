"""Bounded collection cycles; page data and checkpoints commit together."""

import asyncio
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path

from .source_library import SourceFilters, assess, numeric, ranking_product
from .pipeline_modules.database_work import run as database_work


class AcquisitionError(Exception):
    def __init__(self, reason, diagnostic=None):
        super().__init__(reason)
        self.diagnostic = diagnostic or {}


def keyword_terms(rows):
    counts = Counter()
    stop = {"для", "или", "the", "and", "with", "from", "этот", "this"}
    for row in rows:
        text = unicodedata.normalize("NFKC", str(row.get("keyword") or "")).lower()
        words = set(re.findall(r"[^\W\d_]{4,}", text)) - stop
        counts.update(words)
    return sorted(counts, key=lambda term: (-counts[term], len(term), term))[:1]


def ranking_query(body, page, filters):
    query = {k: v for k, v in body.items() if k not in ("seed_sku", "seller_id")}
    query.update(page=page, page_size=100, sort_by="sold_count", sort_order="desc")
    for field, parameter in (
        ("price_min", "avg_price_min"),
        ("price_max", "avg_price_max"),
        ("weight_max_g", "weight_max"),
        ("sales_min", "sales_min"),
    ):
        value = getattr(filters, field)
        if value is not None:
            query[parameter] = value
    if filters.pure_fbs:
        query["sales_schema"] = "FBS"
    return query


async def erp_request(path, query, token):
    if not token:
        raise AcquisitionError("authentication")
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).resolve().parents[1] / "bridges/source-library.mjs"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ | {"MAOZI_ACCESS_TOKEN": token},
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(json.dumps({"path": path, "query": query}).encode()), 100
        )
        result = json.loads(output)
        if not result.get("ok"):
            raise AcquisitionError(result.get("error", "network"), result.get("diagnostic"))
        return result["data"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def page_data(data, page, size):
    if not isinstance(data, (list, dict)):
        raise AcquisitionError("schema")
    rows = data if isinstance(data, list) else data.get("data")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise AcquisitionError("schema")
    if isinstance(data, dict) and data.get("current_page") is not None and int(data["current_page"]) != page:
        raise AcquisitionError("repeated_page")
    last = numeric(data.get("last_page")) if isinstance(data, dict) else None
    if last is None and isinstance(data, dict) and numeric(data.get("total")) is not None:
        per_page = numeric(data.get("per_page"))
        if per_page:
            last = math.ceil(numeric(data["total"]) / per_page)
        # ERP online lists can silently cap a requested 100 rows at 50. Without
        # reported page size, continue until an explicit empty page, never infer
        # exhaustion from the requested size.
    # Only explicit pagination metadata or an empty page proves the traversal ended.
    return rows, last


def archived_state(row):
    if (
        row.get("archived") is True
        or row.get("is_archived") is True
        or row.get("online_status") == "archived"
    ):
        return True
    if row.get("archived") is False or row.get("is_archived") is False:
        return False
    if row.get("online_status") in ("selling", "ready_to_sell", "out_of_stock"):
        return False
    return None


class SourceAcquirer:
    def __init__(self, library, request=erp_request, delists=None):
        self.library = library
        self.request = request
        self.delists = delists
        self.delist_snapshot = None
        self.delist_checked = 0

    async def cycle(self, owner, token, now=None, kinds=None):
        now = time.time() if now is None else now
        def pending_count():
            with self.library.db.connect() as c:
                return c.execute(
                    "SELECT COUNT(*) FROM jobs WHERE owner=? AND (phase NOT IN ('selling','rejected','attention') OR (phase='attention' AND json_extract(data,'$.candidate.source_contract')='flowhub-source-candidates-v1'))",
                    (owner,),
                ).fetchone()[0]
        pending=await database_work(pending_count)
        if pending >= 30:
            return {"state": "backpressure"}
        if self.delist_snapshot is None or now - self.delist_checked >= 60:
            try:
                from .source_delists import read_delists

                self.delist_snapshot = await (self.delists or read_delists)()
                self.delist_checked = now
            except Exception:
                return {"state": "blocked", "reason": "explicit_delists_unavailable"}
        claim_task=asyncio.create_task(asyncio.to_thread(self.library.claim,owner,now,kinds=kinds))
        try:task=await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            task=await claim_task
            if task:
                def release():
                    with self.library.db.connect() as c:
                        c.execute('UPDATE sourcing_tasks SET lease=NULL,lease_until=0 WHERE id=? AND lease=?',(task['id'],task['lease']))
                await database_work(release)
            raise
        if not task:
            return {"state": "idle"}
        try:
            if task["kind"] == "seller":
                return self.evaluate_seller(task, now)
            if task["kind"] == "own_orders":
                return await self.orders(task, token, now)
            if task["kind"] in ("seed", "keyword_lookup", "keyword"):
                with self.library.db.connect() as c:
                    active = c.execute(
                        "SELECT 1 FROM sourcing_seeds s WHERE owner=? AND sku=? AND archived=0 AND checked>? AND NOT EXISTS (SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.sku)",
                        (owner, task["body"].get("sku") or task["body"].get("seed_sku"), now - 21600),
                    ).fetchone()
                if not active:
                    self.library.fail(task, "seed_not_current", now)
                    return {"state": "waiting", "reason": "seed_not_current"}
            if task["kind"] == "keyword_lookup":
                return await self.keywords(task, token, now)
            if task["kind"] == "seller_scan":
                with self.library.db.connect() as c:
                    roots = c.execute(
                        "SELECT sku,shop,offer,body FROM sourcing_seeds s WHERE owner=? AND archived=0 AND checked>? AND json_extract(body,'$.seller_id')=? AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.sku)",
                        (owner, now - 21600, str(task["body"]["seller_id"])),
                    ).fetchall()
                excluded = set(self.delist_snapshot["skus"])
                excluded_offers = {tuple(x) for x in self.delist_snapshot["offers"]}
                task["root_seeds"] = [
                    {"sku": r["sku"], "shop": r["shop"], "offer": r["offer"]}
                    for r in roots
                    if r["sku"] not in excluded
                    and str((json.loads(r["body"]).get("online_evidence") or {}).get("sku")) not in excluded
                    and (r["shop"], r["offer"]) not in excluded_offers
                    and ("*", r["offer"]) not in excluded_offers
                ]
                if not task["root_seeds"]:
                    self.library.fail(task, "seed_not_current", now)
                    return {"state": "waiting", "reason": "seed_not_current"}
            if task["kind"] == "category":
                with self.library.db.connect() as c:
                    active = c.execute(
                        "SELECT 1 FROM sourcing_seeds s WHERE owner=? AND archived=0 AND checked>? AND json_extract(body,'$.category_id')=? AND NOT EXISTS (SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.sku)",
                        (owner, now - 21600, task["body"]["category2"]),
                    ).fetchone()
                if not active:
                    self.library.fail(task, "seed_not_current", now)
                    return {"state": "waiting", "reason": "seed_not_current"}
            if task["kind"] == "own_shop":
                return await self.own_shop(task, token, now)
            query = task["body"] | {"page": task["page"], "page_size": 100}
            if task["kind"] in ("category", "keyword", "seller_scan"):
                with self.library.db.connect() as c:
                    setting = c.execute(
                        "SELECT body FROM sourcing_settings WHERE owner=?", (owner,)
                    ).fetchone()
                filters = SourceFilters(**json.loads(setting[0])) if setting else SourceFilters()
                query = ranking_query(task["body"], task["page"], filters)
            # Existing verified shop expansion covers a category's ranking, not the entire shop.
            seller = task["body"].get("seller_id")
            query.pop("seller_id", None)
            task["request_query"] = query
            task["request_started_at"] = time.time()
            data = await self.request("/api.selection.top/lists", query, token)
            task["response_observed_at"] = time.time()
            task["response"] = data
            rows, last = page_data(data, task["page"], 100)
            if task["kind"] == "seed" and any(str(row.get("sku")) != str(query.get("sku")) for row in rows):
                raise AcquisitionError("identity_mismatch")
            if query.get("category2") and any(
                str(row.get("cate2_id")) != str(query["category2"]) for row in rows
            ):
                raise AcquisitionError("identity_mismatch")
            excluded = set(self.delist_snapshot["skus"])
            selected = [
                r
                for r in rows
                if (not seller or str(r.get("seller_id")) == seller) and str(r.get("sku")) not in excluded
            ]
            products = [ranking_product(row, now) for row in selected]
            if task["kind"] == "seller_scan":
                for product in products:
                    product["source_relation"] = {
                        "kind": "same_seller",
                        "seller_id": seller,
                        "root_seeds": task["root_seeds"],
                        "observed_at": now,
                        "coverage": "ranking-only",
                    }
            return self.library.commit_page(task | {"request_query": query}, rows, products, last, now)
        except Exception as error:
            kind = (
                str(error)
                if str(error)
                in (
                    "authentication",
                    "unsupported",
                    "identity_mismatch",
                    "repeated_page",
                    "schema",
                    "rate_limit",
                )
                else "network"
            )
            self.library.fail(
                task,
                kind,
                now,
                diagnostic=getattr(error, "diagnostic", {}) | {"exception": type(error).__name__},
            )
            with self.library.db.connect() as c:
                state = c.execute("SELECT state FROM sourcing_tasks WHERE id=?", (task["id"],)).fetchone()[0]
            return {"state": "retry" if state == "ready" else state, "reason": kind}

    async def keywords(self, task, token, now):
        data = await self.request("/api.selection.keyword/reverse", {"sku": task["body"]["sku"]}, token)
        rows = data if isinstance(data, list) else data.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise AcquisitionError("schema")
        terms = keyword_terms(rows)
        with self.library.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM sourcing_tasks WHERE id=? AND lease=? AND lease_until>?",
                (task["id"], task["lease"], now),
            ).fetchone():
                raise AcquisitionError("lease_expired")
            for seed in c.execute(
                "SELECT shop,offer,body FROM sourcing_seeds WHERE owner=? AND sku=?",
                (task["owner"], task["body"]["sku"]),
            ).fetchall():
                body = json.loads(seed["body"]) | {
                    "keyword_evidence": {"rows": rows, "terms": terms, "observed_at": now}
                }
                c.execute(
                    "UPDATE sourcing_seeds SET body=? WHERE owner=? AND shop=? AND offer=?",
                    (json.dumps(body), task["owner"], seed["shop"], seed["offer"]),
                )
            for term in terms:
                for mode in ("china", "hot"):
                    self.library.enqueue(
                        task["owner"],
                        "keyword",
                        {"seed_sku": task["body"]["sku"], "name": term, "mainType": mode},
                        25,
                        c,
                    )
            c.execute(
                "UPDATE sourcing_tasks SET due=?,lease=NULL,lease_until=0,successes=successes+1,failures=0,error=NULL,last_at=? WHERE id=?",
                (now + 86400, now, task["id"]),
            )
        return {"kind": "keyword_lookup", "terms": terms, "coverage": "keyword-ranking-only"}

    async def own_shop(self, task, token, now):
        shop = str(task["body"]["shop_id"])
        data = await self.request(
            "/api.product.online/lists",
            {"shop_id": shop, "page": task["page"], "page_size": 100, "archived_type": "all"},
            token,
        )
        rows, last = page_data(data, task["page"], 100)
        if any(str(r.get("shop_id")) != shop for r in rows):
            raise AcquisitionError("identity_mismatch")
        # Commit the observed identities and cursor in one transaction.
        from .source_library import fingerprint

        signature = fingerprint(sorted(str(r.get("id")) for r in rows))
        with self.library.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM sourcing_tasks WHERE id=? AND lease=? AND lease_until>?",
                (task["id"], task["lease"], now),
            ).fetchone():
                raise AcquisitionError("lease_expired")
            if (
                rows
                and c.execute(
                    "SELECT 1 FROM sourcing_pages WHERE task=? AND page<>? AND hash=?",
                    (task["id"], task["page"], signature),
                ).fetchone()
            ):
                raise AcquisitionError("repeated_page")
            for row in rows:
                offer = str(row.get("offer_id") or "")
                seed = c.execute(
                    "SELECT * FROM sourcing_seeds WHERE owner=? AND shop=? AND offer=?",
                    (task["owner"], shop, offer),
                ).fetchone()
                if not seed:
                    continue
                body = json.loads(seed["body"])
                archived = archived_state(row)
                sales = numeric(row.get("sold_count"))
                observed_sales = c.execute(
                    "SELECT SUM(quantity) FROM sourcing_orders WHERE owner=? AND shop=? AND offer=?",
                    (task["owner"], shop, offer),
                ).fetchone()[0]
                if observed_sales is not None:
                    sales = max(sales or 0, observed_sales)
                body["sales_basis"] = (
                    "observed_order_units_lower_bound"
                    if observed_sales is not None
                    else "online_sold_count"
                    if sales is not None
                    else "unknown"
                )
                body["online_evidence"] = {
                    k: row.get(k)
                    for k in (
                        "id",
                        "sku",
                        "shop_id",
                        "offer_id",
                        "online_status",
                        "archived",
                        "is_archived",
                        "sold_count",
                    )
                }
                body["online_evidence"]["observed_at"] = now
                c.execute(
                    "UPDATE sourcing_seeds SET body=?,archived=?,sales=?,checked=? WHERE owner=? AND shop=? AND offer=?",
                    (json.dumps(body), archived, sales, now, task["owner"], shop, offer),
                )
                blocked = c.execute(
                    "SELECT 1 FROM blocks WHERE owner=? AND source_key=?", (task["owner"], seed["sku"])
                ).fetchone()
                excluded = set(self.delist_snapshot["skus"])
                excluded_offers = {tuple(x) for x in self.delist_snapshot["offers"]}
                explicit_delist = (
                    seed["sku"] in excluded
                    or str(row.get("sku")) in excluded
                    or (shop, offer) in excluded_offers
                    or ("*", offer) in excluded_offers
                )
                if explicit_delist:
                    c.execute(
                        "UPDATE sourcing_seeds SET archived=NULL WHERE owner=? AND shop=? AND offer=?",
                        (task["owner"], shop, offer),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO blocks VALUES(?,?,?)",
                        (task["owner"], seed["sku"], "飞书明确下架清单"),
                    )
                if archived is not False or blocked or explicit_delist:
                    continue
                priority = 50 + min(sales or 0, 40)
                self.library.enqueue(task["owner"], "keyword_lookup", {"sku": seed["sku"]}, priority - 20, c)
                for main_type in ("china", "hot", "potential", "blue"):
                    self.library.enqueue(
                        task["owner"], "seed", {"sku": seed["sku"], "mainType": main_type}, priority, c
                    )
                if body.get("category_id"):
                    self.library.enqueue(
                        task["owner"],
                        "category",
                        {"category2": body["category_id"], "mainType": "china"},
                        priority - 30,
                        c,
                    )
            c.execute(
                "INSERT OR REPLACE INTO sourcing_pages VALUES(?,?,?)", (task["id"], task["page"], signature)
            )
            done = not rows or last is not None and task["page"] >= last
            if done:
                c.execute("DELETE FROM sourcing_pages WHERE task=?", (task["id"],))
            c.execute(
                "UPDATE sourcing_tasks SET page=?,due=?,lease=NULL,lease_until=0,failures=0,successes=successes+1,last_at=? WHERE id=?",
                (1 if done else task["page"] + 1, now + (21600 if done else 5), now, task["id"]),
            )
        return {"rows": len(rows), "finished": done, "kind": "own_shop"}

    async def orders(self, task, token, now):
        shop = str(task["body"]["shop_id"])
        data = await self.request(
            "/api.order.ozon/lists", {"shop_id": shop, "page": task["page"], "page_size": 100}, token
        )
        rows, last = page_data(data, task["page"], 100)
        if any(str(r.get("shop_id")) != shop for r in rows):
            raise AcquisitionError("identity_mismatch")
        from .source_library import fingerprint

        signature = fingerprint(sorted(str(r.get("posting_number")) for r in rows))
        with self.library.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM sourcing_tasks WHERE id=? AND lease=? AND lease_until>?",
                (task["id"], task["lease"], now),
            ).fetchone():
                raise AcquisitionError("lease_expired")
            if (
                rows
                and c.execute(
                    "SELECT 1 FROM sourcing_pages WHERE task=? AND page<>? AND hash=?",
                    (task["id"], task["page"], signature),
                ).fetchone()
            ):
                raise AcquisitionError("repeated_page")
            for row in rows:
                posting = str(row.get("posting_number") or "")
                if not posting or not isinstance(row.get("products"), list):
                    raise AcquisitionError("schema")
                c.execute(
                    "DELETE FROM sourcing_orders WHERE owner=? AND shop=? AND posting=?",
                    (task["owner"], shop, posting),
                )
                for product in row["products"]:
                    quantity = numeric(product.get("quantity"))
                    if row.get("status") != "delivered" or quantity is None or not product.get("offer_id"):
                        continue
                    c.execute(
                        "INSERT INTO sourcing_orders VALUES(?,?,?,?,?) ON CONFLICT(owner,shop,posting,offer) DO UPDATE SET quantity=quantity+excluded.quantity",
                        (task["owner"], shop, posting, str(product["offer_id"]), quantity),
                    )
            c.execute(
                "UPDATE sourcing_seeds SET sales=(SELECT SUM(quantity) FROM sourcing_orders o WHERE o.owner=sourcing_seeds.owner AND o.shop=sourcing_seeds.shop AND o.offer=sourcing_seeds.offer) WHERE owner=? AND shop=?",
                (task["owner"], shop),
            )
            for seed in c.execute(
                "SELECT sku,sales FROM sourcing_seeds WHERE owner=? AND shop=? AND archived=0 AND checked>? AND sales>0",
                (task["owner"], shop, now - 21600),
            ).fetchall():
                for main_type in ("china", "hot", "potential", "blue"):
                    self.library.enqueue(
                        task["owner"],
                        "seed",
                        {"sku": seed["sku"], "mainType": main_type},
                        50 + min(seed["sales"], 40),
                        c,
                    )
            c.execute(
                "INSERT OR REPLACE INTO sourcing_pages VALUES(?,?,?)", (task["id"], task["page"], signature)
            )
            done = not rows or last is not None and task["page"] >= last
            if done:
                c.execute("DELETE FROM sourcing_pages WHERE task=?", (task["id"],))
            c.execute(
                "UPDATE sourcing_tasks SET page=?,due=?,lease=NULL,lease_until=0,failures=0,successes=successes+1,last_at=? WHERE id=?",
                (1 if done else task["page"] + 1, now + (21600 if done else 5), now, task["id"]),
            )
        return {"rows": len(rows), "finished": done, "kind": "own_orders"}

    def evaluate_seller(self, task, now):
        seller = str(task["body"]["seller_id"])
        with self.library.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM sourcing_tasks WHERE id=? AND lease=? AND lease_until>?",
                (task["id"], task["lease"], now),
            ).fetchone():
                raise AcquisitionError("lease_expired")
            settings = c.execute(
                "SELECT body FROM sourcing_settings WHERE owner=?", (task["owner"],)
            ).fetchone()
            filters = SourceFilters(**json.loads(settings[0])) if settings else SourceFilters()
            rows = c.execute(
                "SELECT body FROM sourcing_products p WHERE owner=? AND seller=? AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=p.owner AND b.source_key=p.sku)",
                (task["owner"], seller),
            ).fetchall()
            counts = {"qualified": 0, "needs_review": 0, "rejected": 0}
            for row in rows:
                p = json.loads(row["body"])
                decision = assess(p, filters, now)
                counts[decision["state"]] += 1
                if decision["state"] == "qualified" and p.get("category_id"):
                    self.library.enqueue(
                        task["owner"],
                        "category",
                        {"category2": p["category_id"], "mainType": "china"},
                        min(40, task["priority"] + 5),
                        c,
                    )
            summary = {
                "sample_products": len(rows),
                "sample_assessment": counts,
                "coverage": "observed-ranking-products-only",
                "state": "qualified"
                if counts["qualified"]
                else "needs_review"
                if counts["needs_review"]
                else "rejected",
            }
            c.execute(
                "INSERT OR REPLACE INTO sourcing_sellers VALUES(?,?,?,?)",
                (task["owner"], seller, json.dumps(summary), now),
            )
            c.execute(
                "UPDATE sourcing_tasks SET due=?,lease=NULL,lease_until=0,failures=0,successes=successes+1,last_at=? WHERE id=?",
                (now + 21600, now, task["id"]),
            )
        return {"kind": "seller", "seller_id": seller, "assessment": summary}
