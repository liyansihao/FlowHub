import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from flowhub import official_api, plugin_pipeline
from flowhub.official_status import OzonSellerStatusAdapter
from flowhub.pipeline_modules import control, isolation
from tests.test_modular_pipeline import setup


def enable(db):
    (db.directory / isolation.POLICY_FILE).write_text('{"enabled":true}')


def put(db, sku, state, body):
    with db.connect() as c:
        c.execute('UPDATE plugin_pipeline SET state=?,body=?,due=0 WHERE sku=?', (state,json.dumps(body),sku))


def test_cleanup_preserves_unknown_intent_and_lease_and_is_idempotent(tmp_path):
    db, owner = setup(tmp_path, 4); enable(db)
    old = {'requested_at':1,'offer_id':'immutable','submitted':True,'phase':'submitting'}
    put(db,'1','publishing',old)
    put(db,'2','needs_fields',{'repair_retry':{'total_attempts':3}})
    put(db,'3','needs_fields',{'requested_at':1})
    put(db,'4','selling',old)
    with db.connect() as c:
        c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'3','2','busy',20000))
    assert isolation.cleanup(db,now=10000)['isolated']==2
    assert isolation.cleanup(db,now=10001)['isolated']==0
    with db.connect() as c:
        q=c.execute("SELECT * FROM plugin_pipeline WHERE sku='1'").fetchone()
        assert q['state']=='quarantined'
        b=json.loads(q['body']); assert b['offer_id']=='immutable' and b['submitted'] is True
        receipt=c.execute("SELECT previous_body FROM queue_isolation_receipts WHERE sku='1'").fetchone()
        assert json.loads(receipt[0])==old
        assert c.execute("SELECT state FROM plugin_pipeline WHERE sku='3'").fetchone()[0]=='needs_fields'


def test_disabled_paused_and_grace_are_respected(tmp_path):
    db,_=setup(tmp_path);put(db,'1','needs_fields',{'requested_at':1})
    assert isolation.cleanup(db,now=10000)['isolated']==0
    enable(db);control.set_paused(db,'seed',True)
    assert isolation.cleanup(db,now=10000)['isolated']==0
    control.set_paused(db,'seed',False)
    put(db,'1','needs_fields',{'requested_at':1,'isolation_recovery_until':11000})
    assert isolation.cleanup(db,now=10000)['isolated']==0
    assert isolation.cleanup(db,now=12000)['isolated']==1


async def test_normal_lanes_never_dispatch_isolated_tasks(tmp_path,monkeypatch):
    db,_=setup(tmp_path);enable(db)
    put(db,'1','awaiting_remote',{'phase':'manual_review','requested_at':1})
    isolation.cleanup(db)
    advance=AsyncMock();monkeypatch.setattr(plugin_pipeline,'advance',advance)
    for lane in [None,'review','submit','reconcile','reconcile_history','seed_repair']:
        assert not await plugin_pipeline.tick(db,lane=lane)
    advance.assert_not_called()


async def test_transport_budget_persists_and_remote_writes_never_dispatch(tmp_path):
    db,_=setup(tmp_path);official_api.schema(db)
    with db.connect() as c:
        isolation.schema(c)
        c.execute("INSERT INTO official_api_metrics VALUES('a','/v3/product/info/list',9,0,0,0,0)")
    seen=[]
    def handler(r):
        seen.append(r.url.path)
        return httpx.Response(200,json={})
    transport=isolation.ReadBudgetTransport(db,httpx.MockTransport(handler))
    request=httpx.Request('POST','https://api-seller.ozon.ru/v3/product/info/list',json={})
    await transport.handle_async_request(request)
    # Metrics update after network dispatch as in the actual outer official transport.
    with db.connect() as c:c.execute('UPDATE official_api_metrics SET calls=calls+1')
    restarted=isolation.ReadBudgetTransport(db,httpx.MockTransport(handler))
    with pytest.raises(isolation.BudgetDeferred):await restarted.handle_async_request(request)
    with pytest.raises(ValueError,match='write_forbidden'):
        await restarted.handle_async_request(httpx.Request('POST','https://api-seller.ozon.ru/v2/products/stocks',json={}))
    assert len(seen)==1


async def test_busy_foreground_reduces_budget(tmp_path):
    db,_=setup(tmp_path,8);official_api.schema(db)
    with db.connect() as c:
        isolation.schema(c)
        c.execute("INSERT INTO official_api_metrics VALUES('a','read',18,0,0,0,0)")
    transport=isolation.ReadBudgetTransport(db,httpx.MockTransport(lambda r:httpx.Response(200)))
    with pytest.raises(isolation.BudgetDeferred):
        await transport.handle_async_request(httpx.Request('POST','https://api-seller.ozon.ru/v3/product/info/list'))


async def test_isolated_tick_cannot_repeat_recovery_or_overwrite_changed_state(tmp_path,monkeypatch):
    db,_=setup(tmp_path);enable(db)
    put(db,'1','awaiting_remote',{'phase':'manual_review','requested_at':1})
    isolation.cleanup(db)
    monkeypatch.setattr(isolation,'inspect',AsyncMock(return_value={'result':'selling_stock_confirmed','resume':True}))
    assert await isolation.tick(db)
    with db.connect() as c:
        r=c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
        assert r['state']=='awaiting_remote'
        body=json.loads(r['body']);assert body['isolation_recovery_attempted']
        body['isolation_recovery_until']=0
    put(db,'1','awaiting_remote',body);isolation.cleanup(db)
    with db.connect() as c:c.execute('UPDATE queue_isolation_budget SET last_scan=0')
    assert await isolation.tick(db)
    with db.connect() as c:assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='quarantined'


@pytest.mark.parametrize('change,expected',[
    ({},'ready_to_sell'),
    ({'statuses':{'moderate_status':'pending'}},'price_sent'),
    ({'statuses':{'validation_status':'failed'}},'price_sent'),
    ({'statuses':{'is_created':False}},'price_sent'),
    ({'sku':0},'price_sent'),
    ({'errors':[{'code':'some_image_failed','level':'ERROR_LEVEL_WARNING'}]},'price_sent'),
    ({'is_archived':True},'archived'),
])
def test_official_readiness_requires_full_evidence(change,expected):
    row={'id':1,'offer_id':'same','sku':2,'statuses':{'status':'price_sent','moderate_status':'approved',
         'validation_status':'success','is_created':True,'status_name':'Готов к продаже'}}
    status_change=change.pop('statuses',{})
    row.update(change);row['statuses'].update(status_change)
    client=httpx.AsyncClient(base_url='https://api-seller.ozon.ru')
    port=OzonSellerStatusAdapter(client,shop_id='1',warehouse_id='2')
    product=port._product(row)
    assert product.status==expected and product.status!='selling'


async def test_isolated_cancellation_releases_lease_without_resetting_intent(tmp_path,monkeypatch):
    db,_=setup(tmp_path);enable(db)
    put(db,'1','awaiting_remote',{'phase':'manual_review','offer_id':'original','requested_at':1})
    isolation.cleanup(db);began=asyncio.Event()
    async def blocked(*args):
        began.set();await asyncio.Event().wait()
    monkeypatch.setattr(isolation,'inspect',blocked)
    task=asyncio.create_task(isolation.tick(db));await began.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM plugin_pipeline_leases').fetchone()[0]==0
        r=c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
        assert r['state']=='quarantined' and json.loads(r['body'])['offer_id']=='original'


async def test_isolated_late_result_does_not_overwrite_user_action(tmp_path,monkeypatch):
    db,_=setup(tmp_path);enable(db)
    put(db,'1','awaiting_remote',{'phase':'manual_review','requested_at':1});isolation.cleanup(db)
    async def user_changed(*args):
        with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='delisting'")
        return {'resume':True,'result':'selling_stock_confirmed'}
    monkeypatch.setattr(isolation,'inspect',user_changed)
    assert not await isolation.tick(db)
    with db.connect() as c:assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='delisting'


async def test_idle_budget_is_bounded_without_starving_confirmations(tmp_path):
    db,_=setup(tmp_path);official_api.schema(db)
    with db.connect() as c:
        isolation.schema(c)
        c.execute("UPDATE plugin_pipeline SET state='quarantined'")
    transport=isolation.ReadBudgetTransport(db,httpx.MockTransport(lambda r:httpx.Response(200)))
    for _ in range(3):
        await transport.handle_async_request(httpx.Request('POST','https://api-seller.ozon.ru/v3/product/info/list'))
    with pytest.raises(isolation.BudgetDeferred):
        await transport.handle_async_request(httpx.Request('POST','https://api-seller.ozon.ru/v3/product/info/list'))


def test_verified_closeout_updates_original_journal_and_records_once(tmp_path):
    import sqlite3

    from flowhub.source_library import SourceLibrary
    db,owner=setup(tmp_path);library=SourceLibrary(db)
    plan={'shop_id':'shop','warehouse_id':'wh','sku':'1','purpose':'production','stock_target':99}
    record={'plan':plan,'offer_id':'original','store_id':'store','phase':'manual_review'}
    original=json.dumps(record)
    with sqlite3.connect(db.directory/'plugin-production.sqlite3') as j:
        j.execute('CREATE TABLE zero_stock_tests(offer_id TEXT,plan TEXT,phase TEXT,details TEXT,updated_at TEXT)')
        j.execute('INSERT INTO zero_stock_tests VALUES(?,?,?,?,?)',('original',json.dumps(plan),'manual_review','{}',''))
    with db.connect() as c:
        c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL)')
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?,?)',(owner,'1','2',original,0))
    result={'publication_body':original,'product':{'offer_id':'original','shop_id':'shop','status':'selling','product_id':'99'},
            'stocks':[{'warehouse_id':'wh','present':99}]}
    body={'isolation':{'reason':'old'}}
    with db.connect() as c:
        assert isolation.close_verified(c,db,(owner,'1','2'),result,body,library)
    with db.connect() as c:
        assert not isolation.close_verified(c,db,(owner,'1','2'),result,body,library)
        p=json.loads(c.execute('SELECT body FROM plugin_publications').fetchone()[0])
        assert p['verified'] and len(p['events'])==1
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    with sqlite3.connect(db.directory/'plugin-production.sqlite3') as j:
        assert j.execute('SELECT phase FROM zero_stock_tests').fetchone()[0]=='stock_verified'
    assert 'isolation' not in body and body['verified']
