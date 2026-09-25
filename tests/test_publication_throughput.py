import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from flowhub import plugin_publication  # Load the existing FlowEF adapter path.
from flowhub.pipeline_modules.transport import StepTransport
from flowhub.pipeline_modules import request_bridge
from flowef.application.errors import WriteOutcomeUnknown


@pytest.mark.asyncio
async def test_identical_reads_coalesce_but_never_cross_accounts():
    started=asyncio.Event();release=asyncio.Event();calls=[]
    async def handler(r):
        calls.append(r.url.path);started.set();await release.wait()
        return httpx.Response(200,json={'code':1,'data':[]})
    a=httpx.AsyncClient(base_url='https://api.maozierp.com',transport=StepTransport(httpx.MockTransport(handler),namespace='a'))
    b=httpx.AsyncClient(base_url='https://api.maozierp.com',transport=StepTransport(httpx.MockTransport(handler),namespace='a'))
    c=httpx.AsyncClient(base_url='https://api.maozierp.com',transport=StepTransport(httpx.MockTransport(handler),namespace='b'))
    tasks=[asyncio.create_task(x.get('/api.product.favorite/lists?sku=1')) for x in (a,b,c)]
    await started.wait();await asyncio.sleep(.01);release.set();await asyncio.gather(*tasks)
    assert len(calls)==2
    await asyncio.gather(*(x.aclose() for x in (a,b,c)))


@pytest.mark.asyncio
async def test_stock_write_preserves_shop_cache_but_favorite_write_invalidates_read():
    calls=[]
    async def handler(r):calls.append(r.url.path);return httpx.Response(200,json={'code':1,'data':[]})
    t=StepTransport(httpx.MockTransport(handler),namespace='scoped')
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=t) as c:
        await c.get('/api.shop/lists');await c.get('/api.product.favorite/lists?sku=1')
        await c.post('/api.product.online/batch_update_stock',json={})
        await c.get('/api.shop/lists')
        await c.post('/api.product.favorite/toggle',json={})
        await c.get('/api.product.favorite/lists?sku=1')
        await c.get('/api.product.online/get_stock');await c.get('/api.product.online/get_stock')
    assert calls.count('/api.shop/lists')==1
    assert calls.count('/api.product.favorite/lists')==2
    assert calls.count('/api.product.online/get_stock')==2


@pytest.mark.asyncio
async def test_write_during_inflight_read_does_not_leave_cached_absence():
    started=asyncio.Event();release=asyncio.Event();reads=[]
    async def handler(r):
        if r.method=='GET':
            reads.append(1)
            if len(reads)==1:started.set();await release.wait()
        return httpx.Response(200,json={'code':1,'data':reads[:]})
    t=StepTransport(httpx.MockTransport(handler),namespace='race')
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=t) as c:
        first=asyncio.create_task(c.get('/api.product.favorite/lists'))
        await started.wait();await c.post('/api.product.favorite/toggle',json={});release.set();await first
        await c.get('/api.product.favorite/lists')
        assert len(reads)==2


@pytest.mark.asyncio
async def test_network_circuit_does_not_send_extra_reads_or_retry_writes():
    calls=[]
    async def handler(r):calls.append(r.method);raise httpx.ConnectTimeout('test')
    t=StepTransport(httpx.MockTransport(handler),namespace='outage')
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=t) as c:
        for _ in range(4):
            with pytest.raises(httpx.TransportError):await c.get('/api.product.online/lists')
        assert calls==['GET']*3
        with pytest.raises(httpx.TransportError):await c.post('/api.selection.follow/import',json={})
        assert calls==['GET']*3+['POST']


class Process:
    def __init__(self,broken=False):
        self.returncode=None;self.payload=None;self.writes=0;self.broken=broken
        self.stdin=SimpleNamespace(write=self.write,drain=AsyncMock())
        self.stdout=SimpleNamespace(readline=self.readline)
    def write(self,data):self.payload=json.loads(data);self.writes+=1
    async def readline(self):
        if self.broken:return b''
        return json.dumps({'id':self.payload['id'],'ok':True,'result':{'status':200,'json':{'code':1}},'timing':{'network_ms':20}}).encode()+b'\n'
    def kill(self):self.returncode=-9
    async def wait(self):return self.returncode


@pytest.mark.asyncio
async def test_native_process_reused_and_unknown_write_not_replayed(monkeypatch):
    await request_bridge.close_requests();processes=[]
    async def spawn(*a,**k):
        p=Process();processes.append(p);return p
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    bridge=request_bridge.PublicationBridge(Path('/workspace'),execute=True,token='test')
    for _ in range(4):await bridge.call('request',path='/api.shop/lists',method='GET')
    assert len(processes)==1 and sum(p.writes for p in processes)==4
    for p in processes:p.broken=True
    with pytest.raises(WriteOutcomeUnknown):await bridge.call('request',path='/api.selection.follow/import',method='POST')
    assert sum(p.writes for p in processes)==5
    assert bridge.last_timing['pool_wait_ms']>=0
    await request_bridge.close_requests()


@pytest.mark.asyncio
async def test_native_erp_proxy_is_opt_in_and_separates_process_pools(monkeypatch):
    await request_bridge.close_requests()
    monkeypatch.delenv('NODE_USE_ENV_PROXY', raising=False)
    processes=[]; environments=[]
    async def spawn(*a, **k):
        processes.append(Process());environments.append(k['env'])
        return processes[-1]
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    direct=request_bridge.PublicationBridge(Path('/workspace'), execute=True, token='test')
    fallback=request_bridge.PublicationBridge(Path('/workspace'), execute=True, token='test', erp_proxy='http://127.0.0.1:7897')
    explicit=request_bridge.PublicationBridge(Path('/workspace'), execute=True, token='test', erp_proxy='http://127.0.0.1:7898')
    for bridge in (direct, fallback, fallback, explicit):
        await bridge.call('request', path='/api.product.import_logs/index', method='GET')
    assert len(processes)==3
    assert [p.writes for p in processes]==[1,2,1]
    assert environments[0].get('NODE_USE_ENV_PROXY') != '1'
    for env, proxy in zip(environments[1:], ('http://127.0.0.1:7897','http://127.0.0.1:7898')):
        assert env['NODE_USE_ENV_PROXY']=='1'
        assert env['HTTPS_PROXY']==env['https_proxy']==proxy
        assert env['NO_PROXY']==env['no_proxy']==''
    await request_bridge.close_requests()


def test_selected_erp_proxy_preserves_explicit_store_precedence(monkeypatch):
    monkeypatch.setenv('FLOWHUB_MAOZI_ERP_PROXY','http://127.0.0.1:7897')
    assert request_bridge.selected_erp_proxy({})=='http://127.0.0.1:7897'
    assert request_bridge.selected_erp_proxy({'erp_proxy':'http://127.0.0.1:7898'})=='http://127.0.0.1:7898'
    monkeypatch.delenv('FLOWHUB_MAOZI_ERP_PROXY')
    assert request_bridge.selected_erp_proxy({}) is None

@pytest.mark.asyncio
async def test_favorite_imported_view_and_truncation_are_explicit():
    from flowhub.pipeline_modules.favorite_lookup import PublicationSourceAdapter
    queries=[]
    async def handler(r):
        imported=r.url.params.get('is_imported');queries.append(imported)
        data=[] if imported=='0' else [{'id':77,'sku':'123','is_imported':1}]
        return httpx.Response(200,json={'code':1,'data':{'data':data,'total':len(data)}})
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=httpx.MockTransport(handler)) as c:
        assert await PublicationSourceAdapter(c).favorite_id('123')=='77'
        assert queries==['0','1']
    async def broken(r):return httpx.Response(200,json={'code':1,'data':{'data':[],'total':3}})
    from flowef.application.errors import ExternalContractError
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=httpx.MockTransport(broken)) as c:
        with pytest.raises(ExternalContractError):await PublicationSourceAdapter(c).favorite_id('123')


@pytest.mark.asyncio
async def test_exhausted_favorite_uses_exact_original_offer_without_new_write(tmp_path):
    from test_favorite_recovery import setup
    from flowhub.pipeline_modules.favorite_recovery import recover_favorite
    journal,plan,offer,port,guard=setup(tmp_path,'manual_review',reason='favorite_visibility_exhausted',unresolved_phase='favorite_pending')
    port.find_product=AsyncMock(return_value=SimpleNamespace(shop_id=plan.shop_id,offer_id=offer))
    await recover_favorite(port,journal,offer,plan,guard,[],now=2000)
    assert journal.read(offer)['phase']=='reconciling'
    port.add_favorite.assert_not_called();guard.assert_not_called()


def test_network_repairs_have_separate_budget_and_keep_business_failures():
    from flowhub.pipeline_modules.repair_retry import classify,schedule
    assert classify({'steps':[{'reason':'source_acquisition:UND_ERR_CONNECT_TIMEOUT'}]})=='network'
    assert classify({'steps':[{'reason':'sku_mismatch'}]})=='identity_mismatch'
    repair={'attempts':2}
    for i in range(8):
        delay,exhausted=schedule(repair,{'failure_class':'network','reason':'timeout'},100+i)
        assert not exhausted and delay<=900
    assert repair['attempts']==2 and repair['dependency_attempts']==8
    _,exhausted=schedule(repair,{'failure_class':'identity_mismatch','reason':'sku_mismatch'},200)
    assert repair['attempts']==3 and 'dependency_since' not in repair


def test_network_backoff_not_parked_as_product_failure(tmp_path):
    from test_repair_cleanup import setup,insert
    from flowhub.pipeline_modules.repair_cleanup import cleanup
    # Use the existing fixture then explicitly preserve an infrastructure wait.
    db,owner=setup(tmp_path);insert(db,owner)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='needs_fields',body=?",(json.dumps({'repair_retry':{'attempts':5,'failure_class':'network','dependency_since':100,'first_attempt_at':1}}),))
    assert cleanup(db,now=2000)['parked']==0


def test_readback_pressure_and_recovery(tmp_path):
    from test_modular_pipeline import setup
    from flowhub.pipeline_modules.throughput_policy import readback_policy
    db,owner=setup(tmp_path,6)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing',body=?,due=200",(json.dumps({'phase':'reconciling'}),))
        assert readback_policy(c,owner,True,100)['reason']=='readback_pressure'
        c.execute("DELETE FROM plugin_pipeline WHERE sku!='1'")
        assert readback_policy(c,owner,True,100)['submission_priority']
        assert not readback_policy(c,owner,True,400)['submission_priority']


def test_second_repair_lane_requires_recent_measured_health():
    import time
    StepTransport.health_samples.clear()
    assert not StepTransport.healthy_for_more_work()
    good={'network_ms':300,'pacing_wait_ms':2000}
    StepTransport.health_samples.extend((time.monotonic(),good.copy()) for _ in range(10))
    assert StepTransport.healthy_for_more_work()
    StepTransport.health_samples.append((time.monotonic(),{'error_type':'ConnectError'}))
    assert not StepTransport.healthy_for_more_work()
    StepTransport.health_samples.clear()


@pytest.mark.asyncio
async def test_local_connection_outage_does_not_disable_healthy_windows_route():
    calls={'local':0,'remote':0}
    async def broken(request):
        calls['local']+=1
        raise httpx.ConnectError('connect timeout',request=request)
    async def healthy(request):
        calls['remote']+=1
        return httpx.Response(200,json={'code':1,'data':[]})
    local=StepTransport(httpx.MockTransport(broken),namespace='same-account-route-test')
    remote=StepTransport(httpx.MockTransport(healthy),namespace='same-account-route-test',circuit_namespace='windows:04')
    url='https://api.maozierp.com/api.product.import_logs/index?sku=123'
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):await local.handle_async_request(httpx.Request('GET',url))
    response=await remote.handle_async_request(httpx.Request('GET',url))
    assert response.status_code==200
    # Remote success must not clear the failing local host's circuit either.
    with pytest.raises(httpx.ConnectError,match='cooling down'):
        await local.handle_async_request(httpx.Request('GET',url))
    assert calls=={'local':3,'remote':1}
