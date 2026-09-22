import asyncio
import json
import time

import pytest
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub import plugin_pipeline as pipeline
from flowhub.pipeline_modules import control
from flowhub.pipeline_modules.dossier import reviewed_snapshot


def setup(tmp_path, count=1):
    db=Database(tmp_path);library=SourceLibrary(db)
    with db.connect() as c:owner=c.execute('SELECT id FROM users').fetchone()[0]
    for i in range(count):
        sku=str(i+1)
        library.put(owner,{'sku':sku,'seller_id':'2','title':'x','collected_at':100},{'channel':'test'})
        pipeline.enqueue(db,owner,sku,'2')
    return db,owner


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_same_sku(tmp_path,monkeypatch):
    db,_=setup(tmp_path);started=asyncio.Event();release=asyncio.Event();calls=[]
    async def evaluate(*args):
        calls.append(args[-2]);started.set();await release.wait();return {'state':'matched'}
    monkeypatch.setattr(pipeline,'evaluate',evaluate)
    task=asyncio.create_task(pipeline.tick(db,lane='review'))
    await started.wait()
    assert not await pipeline.tick(db,lane='review')
    release.set();await task
    assert calls==['1']


@pytest.mark.asyncio
async def test_slow_review_never_blocks_publication_or_stock_readback(tmp_path,monkeypatch):
    db,_=setup(tmp_path,3);started=asyncio.Event();release=asyncio.Event();seen=[]
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing' WHERE sku IN ('2','3')")
        c.execute("UPDATE plugin_pipeline SET body=? WHERE sku='3'",(json.dumps({'phase':'stock_pending'}),))
    async def evaluate(*args):
        started.set();await release.wait();return {'state':'matched'}
    async def advance(*args):seen.append(args[-2]);return {'phase':'stock_verified','verified':True}
    monkeypatch.setattr(pipeline,'evaluate',evaluate);monkeypatch.setattr(pipeline,'advance',advance)
    task=asyncio.create_task(pipeline.tick(db,lane='review'));await started.wait()
    await asyncio.gather(pipeline.tick(db,lane='submit'),pipeline.tick(db,lane='reconcile'))
    assert set(seen)=={'2','3'}
    release.set();await task


@pytest.mark.asyncio
async def test_pause_and_cancel_preserve_resumable_work(tmp_path,monkeypatch):
    db,_=setup(tmp_path)
    control.set_paused(db,'review',True)
    assert not await pipeline.tick(db,lane='review')
    control.set_paused(db,'review',False)
    started=asyncio.Event()
    async def evaluate(*args):started.set();await asyncio.Event().wait()
    monkeypatch.setattr(pipeline,'evaluate',evaluate)
    task=asyncio.create_task(pipeline.tick(db,lane='review'));await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM plugin_pipeline_leases').fetchone()[0]==0
        assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='queued'


@pytest.mark.asyncio
async def test_crashed_lease_expires_without_resetting_business_state(tmp_path,monkeypatch):
    db,owner=setup(tmp_path)
    with db.connect() as c:c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'1','2','old',time.time()+100))
    assert not await pipeline.tick(db,lane='review')
    with db.connect() as c:c.execute('UPDATE plugin_pipeline_leases SET expires=0')
    async def evaluate(*args):return {'state':'matched'}
    monkeypatch.setattr(pipeline,'evaluate',evaluate)
    assert await pipeline.tick(db,lane='review')


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_new_lease(tmp_path,monkeypatch):
    db,owner=setup(tmp_path)
    async def evaluate(*args):
        with db.connect() as c:c.execute("UPDATE plugin_pipeline_leases SET token='replacement'")
        return {'state':'matched'}
    monkeypatch.setattr(pipeline,'evaluate',evaluate)
    assert not await pipeline.tick(db,lane='review')
    with db.connect() as c:assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='queued'


def test_packet_reuse_requires_identity_freshness_and_complete_package():
    p={'contract':'maozi-plugin-sku-detail-v1','sku':'1','observed_at':100,'dimensions_mm':[150,100,10],'weight_g':20,'attributes':[{'id':1}]}
    r={'candidate':{'source_key':'1','title':'a','image':'https://example.com/a.jpg','origin':{'plugin_detail':p}}}
    s=reviewed_snapshot(r,101)
    assert s['detail']['package_length']==150 and s['source']=='reviewed-plugin-packet'
    assert reviewed_snapshot(r,30000) is not None
    assert reviewed_snapshot(r,100+7*86400) is None
    for change in ({'sku':'2'},{'weight_g':float('nan')},{'attributes':[]}):
        r['candidate']['origin']['plugin_detail']=p|change
        assert reviewed_snapshot(r,101) is None


@pytest.mark.asyncio
async def test_publication_locks_allow_different_stores_but_not_same_sku_or_store(tmp_path,monkeypatch):
    from flowhub import plugin_publication as publication
    db,owner=setup(tmp_path,3)
    with db.connect() as c:
        c.execute('CREATE TABLE plugin_routes(owner TEXT,sku TEXT,seller TEXT,store_id TEXT,expires REAL,run_id TEXT)')
        for sku,store in [('1','a'),('2','a'),('3','b')]:
            c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,sku,'2',store,time.time()+60,'test'))
    monkeypatch.setattr(publication,'DATA',tmp_path)
    started=asyncio.Event();release=asyncio.Event();seen=[]
    async def advance(*args):
        seen.append(args[-2])
        if args[-2]=='1':started.set();await release.wait()
        return {}
    monkeypatch.setattr(publication,'_advance',advance)
    from unittest.mock import AsyncMock
    monkeypatch.setattr('flowhub.official_publication.advance_if_selected',AsyncMock(return_value=None))
    task=asyncio.create_task(publication.advance(db,owner,'1','2'));await started.wait()
    for sku in ('1','2'):
        with pytest.raises(BlockingIOError):await publication.advance(db,owner,sku,'2')
    await publication.advance(db,owner,'3','2')
    release.set();await task
    assert seen==['1','3']


@pytest.mark.asyncio
async def test_runtime_starts_configured_independent_worker_counts(tmp_path,monkeypatch):
    from flowhub.pipeline_modules.runtime import run
    db,_=setup(tmp_path);counts={};started=asyncio.Event()
    async def tick(db,lane=None):
        counts[lane]=counts.get(lane,0)+1
        if sum(counts.values())==8:started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(pipeline,'tick',tick)
    task=asyncio.create_task(run(db))
    await asyncio.wait_for(started.wait(),2)
    assert counts=={'seed_repair':1,'review':2,'submit':2,'reconcile':2,'reconcile_history':1}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task


@pytest.mark.asyncio
async def test_seed_module_never_substitutes_ranking_for_whole_store(tmp_path):
    from flowhub.pipeline_modules.seed import SeedModule
    db,owner=setup(tmp_path);module=SeedModule(db);seen=[]
    async def cycle(owner,token,**kwargs):seen.append(kwargs['kinds']);return {'state':'idle'}
    module.acquirer.cycle=cycle
    await module.run(owner,'test-token')
    assert seen==[('own_orders','own_shop')]


@pytest.mark.asyncio
async def test_read_reuse_never_caches_stock_or_survives_write(tmp_path):
    import httpx
    from flowhub.pipeline_modules.transport import StepTransport
    seen=[]
    async def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200,json={'code':1,'data':[]})
    transport=StepTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=transport) as client:
        for _ in range(2):await client.get('/api.product.favorite/lists')
        assert len(seen)==1
        for _ in range(2):await client.get('/api.product.online/get_stock')
        assert len(seen)==3
        await client.post('/api.product.favorite/toggle',json={})
        await client.get('/api.product.favorite/lists')
        assert len(seen)==5


@pytest.mark.asyncio
async def test_ready_to_submit_beats_older_unprepared_backlog(tmp_path,monkeypatch):
    db,_=setup(tmp_path,2)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing',due=0")
        c.execute("UPDATE plugin_pipeline SET body=?,due=1 WHERE sku='2'",(json.dumps({'phase':'ready'}),))
    seen=[]
    async def advance(*args):seen.append(args[-2]);return {'phase':'reconciling','verified':False}
    monkeypatch.setattr(pipeline,'advance',advance)
    await pipeline.tick(db,lane='submit')
    assert seen==['2']


@pytest.mark.asyncio
async def test_price_repair_waits_without_fabricating_price(tmp_path,monkeypatch):
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db,_=setup(tmp_path)
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='needs_fields',body=?",(json.dumps({'error':'price_evidence_stale'}),))
    async def repair(*args):return {'state':'waiting','reason':'fresh_bound_price_unavailable'}
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    await pipeline.tick(db,lane='seed_repair')
    with db.connect() as c:
        r=c.execute('SELECT state,body,due FROM plugin_pipeline').fetchone()
        assert r['state']=='needs_fields' and r['due']>time.time()+250
        assert json.loads(r['body'])['repair_reason']=='fresh_bound_price_unavailable'


@pytest.mark.asyncio
async def test_fresh_price_goes_back_through_review_not_directly_to_publish(tmp_path,monkeypatch):
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db,_=setup(tmp_path)
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='needs_fields'")
    async def repair(*args):return {'state':'ready','reason':'fresh_price_observed'}
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    await pipeline.tick(db,lane='seed_repair')
    with db.connect() as c:assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='queued'


def test_refreshed_review_cannot_silently_change_prepared_economics():
    import copy
    from flowhub.pipeline_modules.dossier import refresh_unchanged_plan,reviewed_snapshot
    detail={'contract':'maozi-plugin-sku-detail-v1','sku':'1','observed_at':time.time(),'dimensions_mm':[150,100,10],'weight_g':20,'attributes':[{'id':1}]}
    latest={'candidate':{'source_key':'1','title':'a','image':'https://example.com/a.jpg','origin':{'plugin_detail':detail}},
            'result':{'supplier_id':'10','purchase':3,'evidence':{'profit':{'sell_price_cny':30}}}}
    record={'review':{'old':True},'snapshot':reviewed_snapshot(latest),'plan':{'supplier_identity':'10','purchase_price_cny':'3','sell_price_cny':'30'}}
    fresh=copy.deepcopy(record);refresh_unchanged_plan(fresh,latest)
    assert fresh['review']==latest and fresh['plan']==record['plan'] and len(fresh['review_revisions'])==1
    changed=copy.deepcopy(latest);changed['result']['purchase']=4
    with pytest.raises(ValueError,match='prepared_plan_repricing_required'):refresh_unchanged_plan(record,changed)
    assert record['review']=={'old':True}


@pytest.mark.asyncio
async def test_manual_history_cannot_occupy_normal_reconcile_lane(tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    db,_=setup(tmp_path)
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='awaiting_remote',body=json_set(body,'$.phase','manual_review')")
    advance=AsyncMock(return_value={'phase':'stock_pending','verified':False})
    monkeypatch.setattr(pipeline,'advance',advance)
    assert not await pipeline.tick(db,lane='reconcile')
    advance.assert_not_awaited()
    assert await pipeline.tick(db,lane='reconcile_history')
    with db.connect() as c:c.execute('UPDATE plugin_pipeline SET due=0')
    advance.return_value={'phase':'stock_verified','verified':True}
    assert await pipeline.tick(db,lane='reconcile')


@pytest.mark.asyncio
async def test_runtime_gives_acquisition_a_separate_lane_during_canary(tmp_path,monkeypatch):
    from flowhub.pipeline_modules.runtime import run
    db,_=setup(tmp_path);seen=[];started=asyncio.Event()
    (tmp_path/'repair-workflow.json').write_text('{"enabled":true}')
    (tmp_path/'acquisition-policy.json').write_text('{"enabled":true,"source_keys":["1"]}')
    async def tick(db,lane=None,**kwargs):
        if lane=='seed_repair':
            seen.append(kwargs)
            if len(seen)==4:started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(pipeline,'tick',tick)
    task=asyncio.create_task(run(db))
    try:
        await asyncio.wait_for(started.wait(),2)
        assert {'repair_kind':'publication','repair_stage':'acquire','acquisition_route':True} in seen
        assert {'repair_kind':'publication','repair_stage':'acquire','acquisition_route':False} in seen
        assert {'repair_kind':'publication','repair_stage':'validate'} in seen
        assert {'repair_kind':'valuation'} in seen
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
