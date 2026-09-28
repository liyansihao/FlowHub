import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'FlowEF-production/src'))
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.pipeline_modules.admission import schema as pipeline_schema
from flowhub.pipeline_modules import favorite_cleanup as old
from flowhub.pipeline_modules import favorite_reclaim as reclaim
from flowhub.pipeline_modules.favorite_lookup import PublicationAdapter, FavoriteCapacityWait
from flowef.application.errors import ExternalContractError


def setup(tmp_path):
    db = Database(tmp_path); SourceLibrary(db); pipeline_schema(db); old.schema(db)
    settings = old.DEFAULTS | {'enabled': True, 'mode': 'reclaim_unused', 'target_ratio': 0,
                              'include_unrecorded': True, 'authorization': 'test', 'threshold_ratio': .8}
    (tmp_path/'favorite-cleanup.json').write_text(json.dumps(settings))
    with db.connect() as c:
        owner = c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('INSERT INTO pipeline_campaigns VALUES(?,?,?,?)', (owner, 1, '{}', 0))
        c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))')
    ctx = {'owner': owner, 'store': {'config': {}, 'credentials': {'erp_token': 'test'}}}
    return db, ctx, settings


def queue(db, ctx, sku='123', state='selling', verified=True):
    with db.connect() as c:
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)', (ctx['owner'], sku, '2', state, '{}', 0, 0))
        if state == 'selling':
            c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?,?)', (ctx['owner'], sku, '2', json.dumps(
                {'phase': 'stock_verified', 'verified': verified, 'offer_id': 'offer'}), time.time()))


class Fake:
    def __init__(self, rows=None, mode='ok'):
        self.rows = rows if rows is not None else [{'id': 7, 'sku': '123', 'is_imported': 1}]
        self.writes = []; self.mode = mode
    async def call(self, path, method='GET', params=None, body=None):
        if method == 'GET':
            rows = [r for r in self.rows if (not params.get('sku') or str(r['sku']) == params['sku'])
                    and ('is_imported' not in params or int(r['is_imported']) == params['is_imported'])]
            start = (params.get('page', 1)-1)*params.get('page_size', 100)
            return {'used': len(self.rows), 'limit': 1, 'total': len(rows), 'data': rows[start:start+params.get('page_size',100)]}
        assert path == '/api.product.favorite/toggle' and body['status'] is False
        self.writes.append(str(body['productInfo']['id']))
        if self.mode == 'unknown':
            raise TimeoutError()
        self.rows = [r for r in self.rows if str(r['id']) != self.writes[-1]]
        if self.mode == 'lost_ack':
            raise TimeoutError()
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['selling', 'rejected', 'unrecorded'])
async def test_reclaims_used_and_unrecorded_without_online_stock_or_draft(tmp_path, state):
    db, ctx, settings = setup(tmp_path)
    if state != 'unrecorded': queue(db, ctx, state=state)
    api = Fake(); before = api.rows[0].copy()
    assert await reclaim.clean_one(db, 'a', ctx, before, settings, api) == 'deleted'
    assert api.writes == ['7']
    with db.connect() as c:
        receipt = c.execute('SELECT * FROM favorite_cleanup_receipts').fetchone()
        proof = db.open(receipt['body'])
        assert Path(proof['archive_path']).is_file()
        assert proof['favorite']['id'] == 7
        if state != 'unrecorded': assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0] == state
    await reclaim.clean_one(db, 'a', ctx, before, settings, api)
    assert len(api.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['publishing', 'needs_fields', 'needs_review', 'quarantined', 'queued'])
async def test_active_consumers_retained(tmp_path, state):
    db, ctx, settings = setup(tmp_path); queue(db, ctx, state=state); api = Fake()
    assert (await reclaim.clean_one(db, 'a', ctx, api.rows[0], settings, api)).startswith('protected:')
    assert api.writes == []


@pytest.mark.asyncio
async def test_other_owner_and_active_lease_and_incomplete_journal_retained(tmp_path):
    db, ctx, settings = setup(tmp_path); queue(db, ctx); api = Fake()
    with db.connect() as c:
        c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)', (ctx['owner'], '123', '2', 'token', time.time()+60))
    assert 'active_lease' in await reclaim.clean_one(db, 'a', ctx, api.rows[0], settings, api)
    with db.connect() as c: c.execute('DELETE FROM plugin_pipeline_leases')
    with sqlite3.connect(tmp_path/'plugin-production.sqlite3') as c:
        c.execute('CREATE TABLE zero_stock_tests(sku TEXT,offer_id TEXT,phase TEXT,details TEXT)')
        c.execute("INSERT INTO zero_stock_tests VALUES('123','offer','submitting','{}')")
    assert 'publication_journal' in await reclaim.clean_one(db, 'a', ctx, api.rows[0], settings, api)
    assert not api.writes


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected', [('lost_ack','deleted'),('unknown','unconfirmed_present')])
async def test_unknown_delete_never_replayed_after_restart(tmp_path, mode, expected):
    db, ctx, settings = setup(tmp_path); api = Fake(mode=mode); fav = dict(api.rows[0])
    assert await reclaim.clean_one(db, 'a', ctx, fav, settings, api) == expected
    db2 = Database.open_existing(tmp_path)
    await reclaim.clean_one(db2, 'a', ctx, fav, settings, api)
    assert len(api.writes) == 1


@pytest.mark.asyncio
async def test_archive_failure_prevents_remote_delete(tmp_path, monkeypatch):
    db, ctx, settings = setup(tmp_path); api = Fake()
    def fail(*a, **kw): raise OSError('disk full')
    monkeypatch.setattr(reclaim, 'save_archive', fail)
    with pytest.raises(OSError): await reclaim.clean_one(db, 'a', ctx, api.rows[0], settings, api)
    assert not api.writes


@pytest.mark.asyncio
async def test_guard_reentrant_only_in_same_task(tmp_path):
    with reclaim.source_guard(tmp_path, '123'):
        with reclaim.source_guard(tmp_path, '123'): pass
        async def contender():
            with pytest.raises(BlockingIOError):
                with reclaim.source_guard(tmp_path, '123'): pass
        await asyncio.create_task(contender())
    with reclaim.source_guard(tmp_path, '123'): pass


@pytest.mark.asyncio
async def test_drain_survives_restart_below_threshold_retains_only_dependencies(tmp_path, monkeypatch):
    db, ctx, settings = setup(tmp_path); queue(db, ctx, sku='222', state='publishing')
    monkeypatch.setattr(reclaim, 'contexts', lambda db: {'a': ctx})
    api = Fake([{'id': i, 'sku': sku, 'is_imported': 1} for i, sku in enumerate(['111','222','333'], 1)])
    settings |= {'max_checks_per_cycle': 1}
    for _ in range(10):
        result = await reclaim.tick(Database.open_existing(tmp_path), settings, lambda ctx: api)
        if result[0]['state'] == 'complete': break
    assert [r['sku'] for r in api.rows] == ['222']
    assert sorted(api.writes) == ['1','3']
    assert result[0]['retained'] == 1


@pytest.mark.asyncio
async def test_rejection_typed_but_unknown_error_not_retryable():
    async def noop(*args): pass
    for message, expected in [('收藏数量已达上限（3000个），请先删除部分收藏', FavoriteCapacityWait), ('其他错误', ExternalContractError)]:
        async def response(request): return httpx.Response(200, json={'code': 0, 'msg': message})
        async with httpx.AsyncClient(base_url='https://api.maozierp.com', transport=httpx.MockTransport(response)) as client:
            port = PublicationAdapter(client, None, noop, noop)
            plan = SimpleNamespace(sku='123', cover_image='https://a.test/a.jpg', sell_price_cny='12', title='test')
            with pytest.raises(expected): await port.add_favorite(plan)


@pytest.mark.asyncio
async def test_account_full_wait_then_capacity_release_allows_same_plan(tmp_path):
    db, ctx, settings = setup(tmp_path); writes = []
    reclaim.observe(db, 'a', {'used':3000,'limit':3000})
    async def response(request):
        writes.append(request.url.path); return httpx.Response(200,json={'code':1,'data':{}})
    async def noop(*args): pass
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=httpx.MockTransport(response)) as client:
        port = PublicationAdapter(client,None,noop,noop);port.favorite_capacity_context=(db,'a')
        plan=SimpleNamespace(sku='123',cover_image='https://a.test/a.jpg',sell_price_cny='12',title='test')
        with pytest.raises(FavoriteCapacityWait): await port.add_favorite(plan)
        assert not writes
        reclaim.observe(db,'a',{'used':2999,'limit':3000})
        await port.add_favorite(plan)
        with pytest.raises(FavoriteCapacityWait): await port.add_favorite(plan)
        assert writes==['/api.product.favorite/toggle']


@pytest.mark.asyncio
async def test_capacity_wait_does_not_increment_pipeline_attempts_or_quarantine(tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    from test_modular_pipeline import setup as pipeline_setup
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules.isolation import classify
    db,owner=pipeline_setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing',body=?,attempts=3", ('{"phase":"prepared"}',))
    monkeypatch.setattr(pipeline,'advance',AsyncMock(side_effect=FavoriteCapacityWait('waiting_favorite_capacity')))
    assert await pipeline.tick(db,lane='submit')
    with db.connect() as c:row=dict(c.execute('SELECT * FROM plugin_pipeline').fetchone())
    body=json.loads(row['body'])
    assert row['attempts']==3 and row['state']=='publishing' and row['due']>time.time()+50
    assert body['dependency_wait']=='waiting_favorite_capacity'
    assert classify(row['state'],body,time.time()+7200) is None


@pytest.mark.asyncio
async def test_full_rejection_restores_prepared_journal_for_later_retry(tmp_path):
    from unittest.mock import AsyncMock
    from test_favorite_recovery import setup as journal_setup
    from flowef.application.services.test_listing import ProductionListingService
    j,p,o,port,guard=journal_setup(tmp_path,phase='prepared')
    j.move(o,'prepared','prepared',waiting_since=None)
    port.add_favorite.side_effect=FavoriteCapacityWait('full')
    service=ProductionListingService(port,j,guard,stock_guard=guard)
    with pytest.raises(FavoriteCapacityWait):await service.advance(o)
    assert j.read(o)['phase']=='prepared' and not j.read(o)['details'].get('waiting_since')
    port.add_favorite.side_effect=None
    await service.advance(o)
    assert j.read(o)['phase']=='favorite_pending'
    assert j.read(o)['details']['favorite_acknowledged_at']


@pytest.mark.parametrize('unsafe',['unknown','submitted','human','other_error','ack','ok'])
def test_historical_recovery_requires_exact_explicit_rejection(unsafe):
    q={'state':'quarantined','body':{'isolation':{'reason':'remote_outcome_or_platform_issue'}}}
    p={'backend':'maozi_follow','events':[{'from':'prepared','error':'ExternalContractError','reason':'Maozi /api.product.favorite/toggle request failed: 收藏数量已达上限（3000个）'}]}
    j={'phase':'manual_review','details':{'favorite_attempts':1,'reason':'reconciliation_timeout','unresolved_phase':'favorite_pending'}}
    if unsafe=='unknown':p['events'][0]['error']='WriteOutcomeUnknown'
    if unsafe=='submitted':q['body']['submitted']=True
    if unsafe=='human':q['body']['repair_manual']=True
    if unsafe=='other_error':p['events'].append({'error':'TimeoutError'})
    if unsafe=='ack':j['details']['favorite_acknowledged_at']=1
    assert reclaim.capacity_recovery_candidate(q,p,j)==(unsafe=='ok')


@pytest.mark.asyncio
async def test_canary_budget_persists_and_release_resumes_original_batch(tmp_path,monkeypatch):
    db,ctx,settings=setup(tmp_path)
    monkeypatch.setattr(reclaim,'contexts',lambda db:{'a':ctx})
    api=Fake([{'id':i,'sku':str(100+i),'is_imported':1} for i in range(1,4)])
    for _ in range(2):
        result=await reclaim.tick(db,settings|{'verification_delete_limit':1},lambda ctx:api)
        assert result[0]['state']=='canary_hold'
        assert len(api.writes)==1
    await reclaim.tick(db,settings,lambda ctx:api)
    assert len(api.writes)==3 and not api.rows


@pytest.mark.asyncio
async def test_shared_sku_waiting_in_other_seller_is_not_released(tmp_path):
    db,ctx,settings=setup(tmp_path);queue(db,ctx)
    with db.connect() as c:
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(ctx['owner'],'123','other','publishing','{}',0,0))
    api=Fake()
    assert 'pipeline:publishing' in await reclaim.clean_one(db,'a',ctx,api.rows[0],settings,api)
    assert not api.writes


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['ok','unknown'])
async def test_unrecorded_duplicate_sku_group_archived_without_replaying_unknown(tmp_path,mode):
    db,ctx,settings=setup(tmp_path)
    rows=[{'id':7,'sku':'123','is_imported':0},{'id':8,'sku':'123','is_imported':0}]
    api=Fake(list(rows),mode=mode)
    await reclaim.clean_one(db,'a',ctx,rows[0],settings,api)
    await reclaim.clean_one(db,'a',ctx,rows[1],settings,api)
    if mode=='unknown':assert api.writes==['7'] and len(api.rows)==2
    else:assert api.writes==['7','8'] and not api.rows
    with db.connect() as c:
        r=c.execute("SELECT body FROM favorite_cleanup_receipts WHERE favorite_id='7'").fetchone()
        proof=db.open(r[0]);assert {x['id'] for x in proof['favorite_group']}=={7,8}
