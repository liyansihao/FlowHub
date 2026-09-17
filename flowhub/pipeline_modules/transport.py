"""Bounded read reuse, in-flight coalescing and account-scoped network backoff."""
import asyncio
from collections import deque
import time
import httpx


class StepTransport(httpx.AsyncBaseTransport):
    CACHEABLE = {'/api.shop/lists', '/api.product.favorite/lists', '/api.product.import_logs/index'}
    shared_shops = {}
    inflight = {}
    generations = {}
    circuits = {}
    health_samples = deque(maxlen=200)

    @classmethod
    def healthy_for_more_work(cls):
        rows=[r for at,r in cls.health_samples if time.monotonic()-at<180]
        if len(rows)<10 or any(r.get("error_type") for r in rows[-10:]):return False
        return all(r.get("network_ms",float("inf"))<5000 and r.get("pacing_wait_ms",float("inf"))<15000 for r in rows[-10:])

    def __init__(self, transport, ttl=30, namespace=None):
        self.transport=transport;self.ttl=ttl;self.cache={};self.timings=[]
        self.namespace=namespace if namespace is not None else object()

    def scope(self,path):return (asyncio.get_running_loop(),self.namespace,path)

    def invalidate(self,path):
        # Stock/import writes cannot change shop identity. Unknown endpoints may.
        affected={'/api.product.favorite/lists','/api.product.import_logs/index','/api.product.online/lists'}
        if path=='/api.product.online/batch_update_stock':
            # A stock write changes online stock, not favorite/import identity.
            # Keep unrelated reads only for their original TTL; get_stock is
            # never cached or coalesced and remains a fresh verification.
            affected={'/api.product.online/lists'}
        if path not in ('/api.product.favorite/toggle','/api.selection.follow/import',
                        '/api.product.online/batch_update_stock','/api.product.online/sync_shop'):
            affected.add('/api.shop/lists')
        for read in affected:
            scope=self.scope(read);self.generations[scope]=self.generations.get(scope,0)+1
        for key in list(self.cache):
            if httpx.URL(key[1]).path in affected:self.cache.pop(key,None)
        if '/api.shop/lists' in affected:
            for key in list(self.shared_shops):
                if key[:2]==self.scope(path)[:2]:self.shared_shops.pop(key,None)

    @staticmethod
    def network_failure(error):
        text=str(error).lower()
        return isinstance(error,(httpx.TransportError,TimeoutError)) or any(x in text for x in
            ('connect_timeout','connecttimeout','connecterror','fetch failed','econnreset','enotfound','network timeout'))

    async def handle_async_request(self, request):
        path=request.url.path;start=time.monotonic();scope=self.scope(path)
        key=(request.method,str(request.url));generation=self.generations.get(scope,0)
        shared_key=(*scope[:2],*key,generation)
        read=request.method=='GET';cacheable=read and path in self.CACHEABLE
        shop=read and path=='/api.shop/lists'
        saved=self.cache.get(key) if cacheable else None
        if shop and not saved:saved=self.shared_shops.get(shared_key)
        ttl=max(self.ttl,60) if shop else self.ttl
        if saved and saved[4]==generation and start-saved[0]<ttl:
            self.timings.append({'path':path,'seconds':0,'cache_hit':True})
            return httpx.Response(saved[1],content=saved[2],headers=saved[3],request=request)
        if not read and path!='/api.chrome/sku3':self.invalidate(path)
        coalesce=read and path!='/api.product.online/get_stock'
        if coalesce and shared_key in self.inflight:
            result=await asyncio.shield(self.inflight[shared_key])
            if generation!=self.generations.get(scope,0):return await self.handle_async_request(request)
            self.timings.append({'path':path,'seconds':round(time.monotonic()-start,3),'cache_hit':False,'coalesced':True})
            return httpx.Response(result[0],content=result[1],headers=result[2],request=request)
        circuit=self.circuits.get(scope,{})
        if read and circuit.get('until',0)>start:
            self.timings.append({'path':path,'seconds':0,'cache_hit':False,'circuit_wait':True})
            raise httpx.ConnectError('ERP endpoint cooling down after connection failures',request=request)
        future=asyncio.get_running_loop().create_future() if coalesce else None
        if future:
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            self.inflight[shared_key]=future
        metric={'path':path,'cache_hit':False}
        try:
            response=await self.transport.handle_async_request(request)
            content=await response.aread()
            metric.update(response.extensions.get('erp_timing',{}))
            if response.status_code in (401,403):self.invalidate('/authentication-failure')
            self.circuits.pop(scope,None)
            if cacheable and response.status_code==200:
                try:ok=response.json().get('code') in (1,'1')
                except (ValueError,AttributeError):ok=False
                if ok and generation==self.generations.get(scope,0):
                    saved=(time.monotonic(),response.status_code,content,dict(response.headers),generation)
                    self.cache[key]=saved
                    if shop:
                        if len(self.shared_shops)>256:self.shared_shops.clear()
                        self.shared_shops[shared_key]=saved
            if future:future.set_result((response.status_code,content,dict(response.headers)))
            return response
        except BaseException as error:
            metric['error_type']=type(error).__name__
            metric.update(getattr(getattr(self.transport,'bridge',None),'last_timing',{}))
            if read and self.network_failure(error):
                failures=circuit.get('failures',0)+1
                self.circuits[scope]={'failures':failures,'until':time.monotonic()+min(120,30*2**min(failures-3,2)) if failures>=3 else 0}
            if future:future.set_exception(error)
            raise
        finally:
            if future:self.inflight.pop(shared_key,None)
            metric['seconds']=round(time.monotonic()-start,3);self.timings.append(metric)
            self.health_samples.append((time.monotonic(),metric.copy()))

    async def aclose(self):await self.transport.aclose()
