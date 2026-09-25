"""Bounded native connections preserve the existing five publication lanes. Unknown commands are never replayed."""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
from flowef.adapters.erp.flowb_bridge import FlowBBridge
from flowef.application.errors import (ExternalContractError, RateLimited, RequestNotSent,
                                      TemporaryExternalError, WriteOutcomeUnknown)

POOLS = {}


def selected_erp_proxy(config):
    return config.get('erp_proxy') or os.environ.get('FLOWHUB_MAOZI_ERP_PROXY') or None


class Slot:
    process = None

    async def close(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
            process.kill()
            await process.wait()


async def close_requests():
    pools=list(POOLS.values());POOLS.clear()
    await asyncio.gather(*(slot.close() for _,slots in pools for slot in slots))


class PublicationBridge(FlowBBridge):
    def __init__(self, workspace, *, execute=False, token=None, erp_proxy=None):
        super().__init__(workspace, execute=execute, token=token)
        self.erp_proxy = erp_proxy

    async def call(self, action, **args):
        if action != 'request':
            return await super().call(action, **args)
        key=(asyncio.get_running_loop(),str(self.workspace),hashlib.sha256(self.token.encode()).hexdigest(),self.erp_proxy)
        if key not in POOLS:
            queue=asyncio.LifoQueue();slots=[Slot() for _ in range(5)]
            for slot in slots:queue.put_nowait(slot)
            POOLS[key]=(queue,slots)
        queue,_=POOLS[key];began=time.monotonic();slot=await queue.get()
        self.last_timing={'pool_wait_ms':round((time.monotonic()-began)*1000,2)}
        dispatched=False
        try:
            if not slot.process or slot.process.returncode is not None:
                env=os.environ|{'FLOWEF_LEGACY_ROOT':str(self.workspace),'MAOZI_ACCESS_TOKEN':self.token,'MAOZI_HTTP_BACKEND':'native','MAOZI_REQUIRE_FEISHU_DELIST':'1'}
                if self.erp_proxy:
                    env.update({'NODE_USE_ENV_PROXY':'1','HTTPS_PROXY':self.erp_proxy,'https_proxy':self.erp_proxy,'NO_PROXY':'','no_proxy':''})
                slot.process=await asyncio.create_subprocess_exec('node',str(Path(__file__).resolve().parents[2]/'bridges/publication-request.mjs'),
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                    limit=16*1024*1024,env=env)
            identity=str(time.monotonic_ns())
            payload={'id':identity,'action':action,'execute':self.execute,**args}
            slot.process.stdin.write(json.dumps(payload).encode()+b'\n');dispatched=True
            await slot.process.stdin.drain()
            raw=await asyncio.wait_for(slot.process.stdout.readline(),240)
            result=json.loads(raw)
            if result.get('id')!=identity:raise ValueError('request identity mismatch')
        except BaseException as error:
            await slot.close()
            if isinstance(error,asyncio.CancelledError):raise
            if dispatched and args.get('method')!='GET' and args.get('path')!='/api.chrome/sku3':
                raise WriteOutcomeUnknown('native bridge response unknown; reconcile original offer') from error
            raise TemporaryExternalError('native bridge unavailable; request not replayed') from error
        finally:
            queue.put_nowait(slot)
        self.last_timing.update(result.get('timing',{}))
        if not result.get('ok'):
            error=result.get('error',{});self.last_timing.update(error_code=error.get('code'),http_status=error.get('status'));message=str(error.get('code','request_failed'))+': '+str(error.get('message',''))
            if error.get('unknown'):raise WriteOutcomeUnknown(message)
            if error.get('status')==429:raise RateLimited(int(error.get('retry_after_ms',60000)/1000))
            if error.get('code') in ('MAOZI_API_PACING_WAIT','MAOZI_PACING_LOCK_TIMEOUT'):raise RequestNotSent(message)
            raise TemporaryExternalError(message)
        return result['result']


class MeasuredTransport(httpx.AsyncBaseTransport):
    def __init__(self, bridge):self.bridge=bridge

    async def handle_async_request(self, request):
        if request.url.host!='api.maozierp.com' or request.url.scheme!='https':
            raise ExternalContractError('Maozi request origin mismatch')
        query={k:request.url.params.get_list(k) for k in request.url.params}
        self.bridge.last_timing={}
        result=await self.bridge.call('request',path=request.url.path,method=request.method,
            query={k:v[0] if len(v)==1 else v for k,v in query.items()},
            body=json.loads(request.content) if request.content and request.content!=b'null' else None)
        return httpx.Response(result['status'],json=result['json'],
            headers={k:v for k,v in result.get('headers',{}).items() if v is not None},
            request=request,extensions={'erp_timing':getattr(self.bridge,'last_timing',{})})
