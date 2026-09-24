"""Seller API scheduling isolated from ERP, with durable credential-scoped cooldowns.

Only immutable metadata is cached. Product/stock observations remain fresh; callers
can batch them explicitly. Writes are dispatched once and are never retried here.
"""

import asyncio
import hashlib
import json
import os
import time
import weakref
from email.utils import parsedate_to_datetime

import httpx


class OfficialDeferred(Exception):
    def __init__(self, until, reason):
        self.until, self.reason = until, reason
        super().__init__(reason)


METADATA_TTL = {
    "/v1/description-category/tree": 21600,
    "/v1/description-category/attribute": 21600,
    "/v1/description-category/attribute/values": 21600,
    "/v1/description-category/attribute/values/search": 21600,
}
READS = set(METADATA_TTL) | {
    "/v2/warehouse/list",
    "/v3/product/info/list",
    "/v4/product/info/limit",
    "/v5/product/info/prices",
    "/v1/product/import/info",
    "/v2/product/info/stocks-by-warehouse/fbs",
}
WRITES = {
    "/v3/product/import",
    "/v2/products/stocks",
    "/v1/product/import/prices",
    "/v1/product/archive",
    "/v1/product/unarchive",
}

# Coalesce only concurrent reads, never cache product/stock decisions.
_BATCHES = weakref.WeakKeyDictionary()


def schema(db):
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS official_api_accounts(
          account TEXT PRIMARY KEY, blocked_until REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS official_api_slots(
          account TEXT, lane TEXT, next_at REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(account,lane));
        CREATE TABLE IF NOT EXISTS official_api_cache(
          account TEXT, cache_key TEXT, expires REAL NOT NULL, body TEXT NOT NULL,
          PRIMARY KEY(account,cache_key));
        CREATE TABLE IF NOT EXISTS official_api_metrics(
          account TEXT, path TEXT, calls INTEGER NOT NULL DEFAULT 0, cache_hits INTEGER NOT NULL DEFAULT 0,
          errors INTEGER NOT NULL DEFAULT 0, network_ms REAL NOT NULL DEFAULT 0, updated REAL NOT NULL,
          PRIMARY KEY(account,path));
        """)


def retry_seconds(header):
    try:
        return max(1, float(header))
    except (TypeError, ValueError):
        try:
            return max(1, parsedate_to_datetime(header).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 60


class OfficialTransport(httpx.AsyncBaseTransport):
    def __init__(self, db, keys, *, transport=None, interval=0.25):
        self.db, self.interval = db, interval
        # Never persist the API key, and changing credentials unblocks only that binding.
        self.account = hashlib.sha256((str(keys["client_id"]) + "\0" + keys["api_key"]).encode()).hexdigest()
        self.inner = transport if transport is not None else httpx.AsyncHTTPTransport(
            retries=0, proxy=os.environ.get("FLOWHUB_OZON_SELLER_PROXY") or None
        )
        schema(db)

    def metric(self, path, *, cached=False, error=False, elapsed=0):
        with self.db.connect() as c:
            c.execute(
                """INSERT INTO official_api_metrics VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(account,path) DO UPDATE SET calls=calls+excluded.calls,
                cache_hits=cache_hits+excluded.cache_hits, errors=errors+excluded.errors,
                network_ms=network_ms+excluded.network_ms, updated=excluded.updated""",
                (self.account, path, int(not cached), int(cached), int(error), elapsed, time.time()),
            )

    async def handle_async_request(self, request):
        if (
            request.url.host != "api-seller.ozon.ru"
            or request.url.scheme != "https"
            or request.method != "POST"
        ):
            raise ValueError("official_api_origin_required")
        try:
            body = json.loads(request.content)
        except (ValueError, TypeError):
            return await self._dispatch(request)
        field = "offer_id" if request.url.path == "/v3/product/info/list" else None
        # Conservatively cap at 100. SKU/stock pagination stays in the strict adapter.
        if (
            not field
            or set(body) != {field}
            or not isinstance(body[field], list)
            or not 1 <= len(body[field]) <= 100
        ):
            return await self._dispatch(request)
        if any(not isinstance(v, str) or not v for v in body[field]):
            return await self._dispatch(request)
        loop = asyncio.get_running_loop()
        queues = _BATCHES.setdefault(loop, {})
        key = (str(self.db.path), self.account, request.url.path)
        future = loop.create_future()
        if key not in queues:
            queues[key] = []
            # A bounded coalescing window does not alter polling intervals.
            loop.call_later(0.01, lambda: asyncio.create_task(self._flush(queues, key)))
        queues[key].append((request, body[field], future))
        return await future

    async def _flush(self, queues, key):
        pending = queues.pop(key, [])
        while pending:
            group = []
            wanted = set()
            while pending:
                request, ids, future = pending[0]
                if future.cancelled():
                    pending.pop(0)
                    continue
                if group and len(wanted | set(ids)) > 100:
                    break
                pending.pop(0)
                group.append((request, ids, future))
                wanted.update(ids)
            if not group:
                continue
            first = group[0][0]
            headers = {k: v for k, v in first.headers.items() if k.lower() != "content-length"}
            combined = httpx.Request(
                "POST",
                first.url,
                headers=headers,
                json={"offer_id": sorted(wanted)},
                extensions=first.extensions,
            )
            try:
                response = await self._dispatch(combined)
                if response.is_success:
                    data = response.json()
                    items = data.get("items")
                    if (
                        not isinstance(items, list)
                        or any(not isinstance(p, dict) or p.get("offer_id") not in wanted for p in items)
                        or len({p["offer_id"] for p in items}) != len(items)
                    ):
                        raise ValueError("official_batch_identity_mismatch")
                for original, ids, future in group:
                    if future.done():
                        continue
                    payload = (
                        (data | {"items": [p for p in items if p["offer_id"] in ids]})
                        if response.is_success
                        else None
                    )
                    result = httpx.Response(
                        response.status_code,
                        headers={
                            k: v
                            for k, v in response.headers.items()
                            if k.lower() not in ("content-length", "content-encoding")
                        },
                        **({"json": payload} if payload is not None else {"content": response.content}),
                        request=original,
                    )
                    future.set_result(result)
                await response.aclose()
            except BaseException as error:
                for _, _, future in group:
                    if not future.done():
                        future.set_exception(error)

    async def _dispatch(self, request):
        if (
            request.url.scheme != "https"
            or request.url.host != "api-seller.ozon.ru"
            or request.method != "POST"
        ):
            raise ValueError("official_api_origin_required")
        path = request.url.path
        if path not in READS | WRITES:
            raise ValueError("unsupported_official_endpoint")
        lane = path  # A read backlog cannot reserve the inventory/import lane.
        cache_key = hashlib.sha256(path.encode() + request.content).hexdigest()
        from .pipeline_modules.database_work import run as database_work
        reservation=await database_work(self._reserve,request,path,lane,cache_key)
        if isinstance(reservation,httpx.Response):return reservation
        await asyncio.sleep(max(0,reservation-time.time()))
        await database_work(self._check_blocked)
        started=time.monotonic()
        try:
            response=await self.inner.handle_async_request(request)
            await response.aread()
        except (httpx.TransportError,asyncio.CancelledError):
            await database_work(self._failed,path,lane,(time.monotonic()-started)*1000)
            raise
        await database_work(self._observed,response,path,lane,cache_key,(time.monotonic()-started)*1000)
        return response

    def _reserve(self,request,path,lane,cache_key):
        now = time.time()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            blocked = c.execute(
                "SELECT * FROM official_api_accounts WHERE account=?", (self.account,)
            ).fetchone()
            if blocked and blocked["blocked_until"] > now:
                raise OfficialDeferred(blocked["blocked_until"], blocked["reason"])
            if path in METADATA_TTL:
                cached = c.execute(
                    "SELECT body FROM official_api_cache WHERE account=? AND cache_key=? AND expires>?",
                    (self.account, cache_key, now),
                ).fetchone()
                if cached:
                    result = self.db.open(cached["body"])
                    # End the transaction before the metrics write.
                    c.commit()
                    self.metric(path, cached=True)
                    return httpx.Response(200, json=result, request=request)
            slot = c.execute(
                "SELECT next_at FROM official_api_slots WHERE account=? AND lane=?", (self.account, lane)
            ).fetchone()
            at = max(now, slot[0] if slot else now)
            if at - now > 2:
                raise OfficialDeferred(at, "official_rate_wait")
            c.execute(
                """INSERT INTO official_api_slots(account,lane,next_at) VALUES(?,?,?)
                ON CONFLICT(account,lane) DO UPDATE SET next_at=excluded.next_at""",
                (self.account, lane, at + self.interval),
            )
        return at

    def _check_blocked(self):
        # Another process can encounter authentication/rate failures during the wait.
        with self.db.connect() as c:
            blocked = c.execute(
                "SELECT * FROM official_api_accounts WHERE account=?", (self.account,)
            ).fetchone()
            if blocked and blocked["blocked_until"] > time.time():
                raise OfficialDeferred(blocked["blocked_until"], blocked["reason"])

    def _failed(self,path,lane,elapsed):
        self.metric(path, error=True, elapsed=elapsed)
        with self.db.connect() as c:
            c.execute(
                "UPDATE official_api_slots SET next_at=MAX(next_at,?),failures=failures+1 WHERE account=? AND lane=?",
                (time.time() + 10, self.account, lane),
            )

    def _observed(self,response,path,lane,cache_key,elapsed):
        self.metric(path, error=response.status_code >= 400, elapsed=elapsed)
        with self.db.connect() as c:
            if response.status_code in (401, 403, 429):
                duration = (
                    300
                    if response.status_code in (401, 403)
                    else retry_seconds(response.headers.get("Retry-After"))
                )
                reason = (
                    "official_authentication" if response.status_code in (401, 403) else "official_rate_limit"
                )
                c.execute(
                    """INSERT INTO official_api_accounts VALUES(?,?,?) ON CONFLICT(account) DO UPDATE
                    SET blocked_until=MAX(blocked_until,excluded.blocked_until),reason=excluded.reason""",
                    (self.account, time.time() + duration, reason),
                )
            elif response.status_code >= 500:
                c.execute(
                    "UPDATE official_api_slots SET next_at=MAX(next_at,?),failures=failures+1 WHERE account=? AND lane=?",
                    (time.time() + 10, self.account, lane),
                )
            elif response.is_success:
                c.execute(
                    "UPDATE official_api_slots SET failures=0 WHERE account=? AND lane=?",
                    (self.account, lane),
                )
                if path in METADATA_TTL:
                    value = response.json()
                    if isinstance(value, dict) and isinstance(value.get("result"), list) and value["result"]:
                        c.execute("DELETE FROM official_api_cache WHERE expires<?", (time.time(),))
                        c.execute(
                            "INSERT OR REPLACE INTO official_api_cache VALUES(?,?,?,?)",
                            (self.account, cache_key, time.time() + METADATA_TTL[path], self.db.seal(value)),
                        )

    async def aclose(self):
        await self.inner.aclose()


def client(db, keys, *, transport=None):
    return httpx.AsyncClient(
        base_url="https://api-seller.ozon.ru",
        headers={"Client-Id": str(keys["client_id"]), "Api-Key": keys["api_key"]},
        transport=OfficialTransport(db, keys, transport=transport),
        timeout=25,
        trust_env=False,
        follow_redirects=False,
    )


async def capacity(api):
    """Normalize BOTH create limits to the existing remaining/limit contract."""
    response = await api.post("/v4/product/info/limit", json={})
    response.raise_for_status()
    data = response.json()
    result = {}
    for name in ("total", "daily_create"):
        part = data.get(name, {})
        limit, usage = part.get("limit"), part.get("usage")
        if type(limit) is not int or type(usage) is not int or min(limit, usage) < 0:
            raise ValueError("official_capacity_incomplete")
        result[name] = f"{max(0, limit - usage)}/{limit}"
    return result
