"""Coalesce identical repair reads and cool down failing endpoints per ERP account."""
import asyncio
import copy
import hashlib
import time
import httpx


class RepairReads:
    cache = {}
    inflight = {}
    failures = {}

    @classmethod
    async def get(cls, api, path, *, params=None):
        account = hashlib.sha256(api.keys['erp_token'].encode()).hexdigest()
        scope = (asyncio.get_running_loop(), account, path)
        key = (*scope, tuple(sorted((params or {}).items())))
        now = time.monotonic()
        cached = cls.cache.get(key)
        if cached and now < cached[0]:
            return copy.deepcopy(cached[1])
        if key in cls.inflight:
            return copy.deepcopy(await asyncio.shield(cls.inflight[key]))
        failures, until = cls.failures.get(scope, (0, 0))
        if now < until:
            raise httpx.ConnectError('repair endpoint cooling down after connection failures')
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        cls.inflight[key] = future
        try:
            result = await api.erp('GET', path, params=params)
            cls.failures.pop(scope, None)
            # Only the currency conversion used to acquire a draft has a short cache.
            # Product facts and quotes retain their own recorded observation times.
            if path == '/api.exchange_rate/index':
                if len(cls.cache) >= 128:
                    cls.cache.clear()
                cls.cache[key] = (time.monotonic() + 60, copy.deepcopy(result))
            future.set_result(result)
            return copy.deepcopy(result)
        except BaseException as error:
            if isinstance(error, (httpx.TransportError, TimeoutError)):
                failures += 1
                cls.failures[scope] = (failures, time.monotonic() + 60 if failures >= 3 else 0)
            future.set_exception(error)
            raise
        finally:
            cls.inflight.pop(key, None)
