import asyncio
import json
import time

import httpx
import pytest
from flowhub import plugin_pipeline as pipeline
from flowhub.pipeline_modules.transport import StepTransport, RemoteReadDeferred
from flowef.adapters.http import request_json
from flowef.application.errors import RequestNotSent


@pytest.mark.asyncio
async def test_concurrent_failures_accumulate_and_single_recovery_probe():
    started = asyncio.Queue()
    release = asyncio.Event()
    calls = []
    failing = True
    async def handler(request):
        calls.append(str(request.url))
        started.put_nowait(True)
        await release.wait()
        if failing:
            raise httpx.ConnectTimeout('injected connection failure')
        return httpx.Response(200, json={'code': 1})
    transport = StepTransport(httpx.MockTransport(handler), namespace=object())
    async with httpx.AsyncClient(base_url='https://api.maozierp.com', transport=transport) as client:
        tasks = [asyncio.create_task(client.get(f'/api.product.online/lists?sku={i}')) for i in range(5)]
        for _ in tasks: await started.get()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(r, httpx.ConnectTimeout) for r in results)
        state = transport.circuits[(*transport.scope('/api.product.online/lists'), 'local')]
        assert state['failures'] == 5
        with pytest.raises(RemoteReadDeferred): await client.get('/api.product.online/lists?sku=6')
        assert len(calls) == 5
        state['until'] = 0
        release.clear()
        failing = False
        probe = asyncio.create_task(client.get('/api.product.online/lists?sku=7'))
        await started.get()
        batch = await asyncio.gather(*(client.get(f'/api.product.online/lists?sku={i}') for i in range(8,108)), return_exceptions=True)
        assert all(isinstance(r, RemoteReadDeferred) for r in batch)
        assert len(calls) == 6
        release.set()
        assert (await probe).status_code == 200
        assert (await client.get('/api.product.online/lists?sku=108')).status_code == 200
        assert state['failures'] == 0 and not state['probe']


@pytest.mark.asyncio
async def test_deferred_read_survives_adapter_without_becoming_connection_error():
    transport = StepTransport(httpx.MockTransport(lambda r: httpx.Response(200)), namespace=object())
    transport.circuits[(*transport.scope('/api.product.online/lists'),'local')] = {
        'failures': 3, 'until': time.monotonic()+30, 'probe': False, 'epoch': 1}
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=transport) as client:
        with pytest.raises(RequestNotSent) as error:
            await request_json(client,'GET','/api.product.online/lists')
    assert type(error.value) is RemoteReadDeferred
    assert transport.timings[-1]['request_sent'] is False


@pytest.mark.asyncio
async def test_cooldown_releases_product_without_failure_budget_or_duplicate_work(tmp_path,monkeypatch):
    from test_modular_pipeline import setup
    db,_ = setup(tmp_path,2)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing',attempts=7,due=0,body=?", (json.dumps({'phase':'ready','submitted':False}),))
    seen = []
    async def advance(db,owner,sku,seller):
        seen.append(sku)
        if sku=='1': raise RemoteReadDeferred('/api.product.import_logs/index',45)
        return {'phase':'stock_verified','verified':True}
    monkeypatch.setattr(pipeline,'advance',advance)
    before=time.time()
    assert await pipeline.tick(db,lane='submit')
    assert await pipeline.tick(db,lane='submit')
    with db.connect() as c:
        row=c.execute("SELECT * FROM plugin_pipeline WHERE sku='1'").fetchone()
        assert row['state']=='publishing' and row['attempts']==7 and row['due']>=before+45
        assert json.loads(row['body'])['phase']=='ready'
        assert c.execute('SELECT count(*) FROM plugin_pipeline_leases').fetchone()[0]==0
        assert c.execute("SELECT state FROM plugin_pipeline WHERE sku='2'").fetchone()[0]=='selling'
        assert c.execute("SELECT count(*) FROM pipeline_module_events WHERE outcome='waiting_dependency'").fetchone()[0]==1
    assert seen==['1','2']


@pytest.mark.asyncio
async def test_cancelled_probe_does_not_leave_endpoint_stuck():
    started=asyncio.Event()
    async def handler(request):
        started.set()
        await asyncio.Event().wait()
    transport=StepTransport(httpx.MockTransport(handler),namespace=object())
    state={'failures':3,'until':0,'probe':False,'epoch':1}
    transport.circuits[(*transport.scope('/api.product.online/lists'),'local')]=state
    task=asyncio.create_task(transport.handle_async_request(httpx.Request('GET','https://api.maozierp.com/api.product.online/lists')))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not state['probe'] and state['failures']==3
