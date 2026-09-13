"""Request timings and short, per-advance reuse of identical read-only checks."""
import time
import httpx


class StepTransport(httpx.AsyncBaseTransport):
    CACHEABLE = {'/api.shop/lists', '/api.product.favorite/lists', '/api.product.import_logs/index'}

    shared_shops = {}

    def __init__(self, transport, ttl=30, namespace=None):
        self.transport=transport;self.ttl=ttl;self.cache={};self.timings=[];self.namespace=namespace

    async def handle_async_request(self, request):
        key=(request.method,str(request.url));start=time.monotonic()
        cacheable=request.method=='GET' and request.url.path in self.CACHEABLE
        shared_key=(self.namespace,*key)
        shared=bool(self.namespace and request.method=='GET' and request.url.path=='/api.shop/lists')
        saved=self.cache.get(key) if cacheable else None
        if shared and not saved:saved=self.shared_shops.get(shared_key)
        if saved and start-saved[0]<self.ttl:
            self.timings.append({'path':request.url.path,'seconds':0,'cache_hit':True})
            return httpx.Response(saved[1],content=saved[2],headers=saved[3],request=request)
        # All writes invalidate prior observations before dispatch, including
        # writes with unknown outcomes. Stock and catalog reads are never cached.
        if request.method!='GET' and request.url.path!='/api.chrome/sku3':
            self.cache.clear()
            for old in list(self.shared_shops):
                if old[0]==self.namespace:self.shared_shops.pop(old,None)
        try:
            response=await self.transport.handle_async_request(request)
            content=await response.aread()
            if cacheable and response.status_code==200:
                try:ok=response.json().get('code') in (1,'1')
                except (ValueError,AttributeError):ok=False
                if ok:
                    self.cache[key]=(time.monotonic(),response.status_code,content,dict(response.headers))
                    if shared:
                        if len(self.shared_shops)>256:self.shared_shops.clear()
                        self.shared_shops[shared_key]=self.cache[key]
            return response
        finally:
            self.timings.append({'path':request.url.path,'seconds':round(time.monotonic()-start,3),'cache_hit':False})

    async def aclose(self):
        await self.transport.aclose()
