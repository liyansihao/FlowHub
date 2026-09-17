import asyncio

import httpx
import pytest

from flowhub.pipeline_modules.runtime import publication_tick
from flowhub.pipeline_modules.transport import StepTransport


@pytest.mark.asyncio
async def test_stock_write_keeps_unrelated_reads_but_import_invalidates_other_worker():
    calls=[]
    async def handler(request):
        calls.append((request.method,request.url.path))
        return httpx.Response(200,json={'code':1,'data':[]})
    a=StepTransport(httpx.MockTransport(handler),namespace='stock-preserves-identity')
    b=StepTransport(httpx.MockTransport(handler),namespace='stock-preserves-identity')
    paths=('/api.product.favorite/lists','/api.product.import_logs/index')
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=a) as client, httpx.AsyncClient(base_url='https://api.maozierp.com',transport=b) as other:
        for path in paths:await client.get(path)
        await other.post('/api.product.online/batch_update_stock',json={})
        for path in paths:await client.get(path)
        assert sum(method=='GET' for method,_ in calls)==2
        await other.post('/api.selection.follow/import',json={})
        for path in paths:await client.get(path)
        assert sum(method=='GET' for method,_ in calls)==4
        for _ in range(2):await client.get('/api.product.online/get_stock')
        assert calls.count(('GET','/api.product.online/get_stock'))==2


@pytest.mark.asyncio
async def test_preserved_reads_still_expire(monkeypatch):
    now=[100.];monkeypatch.setattr('flowhub.pipeline_modules.transport.time.monotonic',lambda:now[0])
    calls=[]
    async def handler(request):
        calls.append(request.method)
        return httpx.Response(200,json={'code':1,'data':[]})
    transport=StepTransport(httpx.MockTransport(handler),ttl=15)
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=transport) as client:
        await client.get('/api.product.favorite/lists')
        now[0]+=10
        await client.post('/api.product.online/batch_update_stock',json={})
        now[0]+=6
        await client.get('/api.product.favorite/lists')
    assert calls==['GET','POST','GET']


@pytest.mark.asyncio
@pytest.mark.parametrize('name,index,primary,expected',[
    ('submit',0,True,['submit']),
    ('submit',1,False,['submit','reconcile']),
    ('reconcile',0,False,['reconcile']),
    ('reconcile',1,False,['reconcile','submit']),
    ('reconcile',1,True,['reconcile']),
])
async def test_idle_lending_preserves_primary_work_and_reserved_readback(name,index,primary,expected):
    calls=[]
    async def tick(db,*,lane):
        calls.append(lane)
        return primary if len(calls)==1 else True
    assert await publication_tick(None,name,index,tick)==(primary or len(expected)>1)
    assert calls==expected


@pytest.mark.asyncio
async def test_borrowing_still_obeys_product_lease_and_pause(tmp_path,monkeypatch):
    from test_modular_pipeline import setup
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules import control
    db,_=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing',body='{\"phase\":\"ready\"}',due=0")
    started=asyncio.Event();release=asyncio.Event();calls=[]
    async def advance(*args):
        calls.append(args[-2]);started.set();await release.wait()
        return {'phase':'reconciling','verified':False}
    monkeypatch.setattr(pipeline,'advance',advance)
    borrowed=asyncio.create_task(publication_tick(db,'reconcile',1,pipeline.tick))
    await started.wait()
    assert not await publication_tick(db,'submit',0,pipeline.tick)
    control.set_paused(db,'publication',True)
    assert not await publication_tick(db,'reconcile',1,pipeline.tick)
    release.set();await borrowed
    assert calls==['1']


@pytest.mark.asyncio
async def test_lock_wait_retains_previous_error_but_does_not_report_it_as_new(tmp_path,monkeypatch):
    import json,time
    from test_modular_pipeline import setup
    from flowhub import plugin_pipeline as pipeline
    db,_=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing',attempts=3,due=0,body=?",(json.dumps({'phase':'ready','error':'AuthenticationFailed: historical'}),))
    async def busy(*args):raise BlockingIOError()
    monkeypatch.setattr(pipeline,'advance',busy)
    before=time.time()
    assert not await pipeline.tick(db,lane='submit')
    with db.connect() as c:
        row=c.execute('SELECT state,attempts,due,body FROM plugin_pipeline').fetchone()
        event=c.execute('SELECT outcome,details FROM pipeline_module_events ORDER BY id DESC LIMIT 1').fetchone()
        assert row['state']=='publishing' and row['attempts']==3
        assert row['due']>=before+5
        assert json.loads(row['body'])['error']=='AuthenticationFailed: historical'
        assert event['outcome']=='waiting_lock' and json.loads(event['details'])['reason'] is None
        assert c.execute('SELECT count(*) FROM plugin_pipeline_leases').fetchone()[0]==0
