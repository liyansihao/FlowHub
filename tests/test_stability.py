import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from flowhub import collection_capacity as capacity
from flowhub.db import Database
from flowhub.modules import Pending
from flowhub.source_detail import SourceCollector
from flowhub.pipeline_modules import lifecycle
from flowhub.stability_monitor import transition
from tests.test_draft_cleanup import setup, cleanup


def test_monitor_uses_verified_post_cleanup_capacity(tmp_path):
    import sqlite3
    from flowhub.stability_snapshot import audit
    from flowhub.pipeline_modules import control
    db,owner,_=setup(tmp_path)
    control.schema(db)
    (tmp_path/'collection-capacity.json').write_text('{"enabled":true}')
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_publications(body TEXT)')
    control.record(db,'seed',owner,'*',time.time()-30,'draft_cleanup',{'used_before':1000,'limit':1000})
    capacity.observe(db,'account',{'used':0,'limit':1000},time.time()-1)
    (tmp_path/'cluster').mkdir()
    with sqlite3.connect(tmp_path/'cluster/cluster.sqlite3') as c:
        c.executescript('CREATE TABLE devices(name,last_seen,enabled); CREATE TABLE runtime_reports(component,seen);'
                        'CREATE TABLE compute_jobs(kind,state,created); CREATE TABLE erp_commands(state,created);')
    result=audit(tmp_path)
    assert result['last_collection_capacity_observation']['used']==0
    assert not any(a['code']=='collection_capacity_high_last_observation' for a in result['alerts'])


def collector(tmp_path, sku='123'):
    db = Database(tmp_path)
    (tmp_path / 'collection-capacity.json').write_text('{"enabled":true}')
    context = {'owner': 'o', 'candidate': {'source_key': sku}, 'store': {'credentials': {'erp_token': 'test'}}}
    return SourceCollector(db, context)


async def test_capacity_shared_reservations_and_restart(tmp_path):
    one = collector(tmp_path); two = collector(tmp_path, '456')
    capacity.observe(one.db, capacity.account(one.c), {'used': 949, 'limit': 1000}, time.time())
    one.call = two.call = AsyncMock(side_effect=AssertionError('unexpected read'))
    await capacity.guard(one, reserve=True)
    with pytest.raises(Pending, match='capacity admission'):
        await capacity.guard(two, reserve=True)
    # New objects cannot forget an unknown write or retry it.
    restarted = collector(tmp_path)
    capacity.observe(one.db, capacity.account(one.c), {'used': 100, 'limit': 1000}, time.time())
    with pytest.raises(Pending, match='reservation retained'):
        await capacity.guard(restarted, reserve=True)
    capacity.finish(one, rejected=True)
    await capacity.guard(two, reserve=True)


async def test_capacity_hysteresis_and_observation_order(tmp_path):
    one = collector(tmp_path); scope = capacity.account(one.c); now = time.time()
    capacity.observe(one.db, scope, {'used': 1000, 'limit': 1000}, now)
    capacity.observe(one.db, scope, {'used': 100, 'limit': 1000}, now-1)
    with pytest.raises(Pending): await capacity.guard(one)
    capacity.observe(one.db, scope, {'used': 850, 'limit': 1000}, now+.01)
    await asyncio.sleep(.02)
    with pytest.raises(Pending): await capacity.guard(one)
    capacity.observe(one.db, scope, {'used': 799, 'limit': 1000}, time.time())
    await capacity.guard(one)


async def test_confirmed_reservation_counted_until_later_read(tmp_path):
    one = collector(tmp_path); scope = capacity.account(one.c)
    capacity.observe(one.db, scope, {'used': 10, 'limit': 1000}, time.time())
    await capacity.guard(one, reserve=True)
    read_started = time.time()-1
    capacity.finish(one)
    capacity.observe(one.db, scope, {'used': 11, 'limit': 1000}, read_started)
    with one.db.connect() as c: assert c.execute('SELECT count(*) FROM collection_reservations').fetchone()[0] == 1
    capacity.observe(one.db, scope, {'used': 11, 'limit': 1000}, time.time())
    with one.db.connect() as c: assert c.execute('SELECT count(*) FROM collection_reservations').fetchone()[0] == 0


async def test_existing_detail_read_allowed_while_full(tmp_path):
    one = collector(tmp_path); capacity.observe(one.db, capacity.account(one.c), {'used':1000,'limit':1000}, time.time())
    one.save('draft_ready', {'source_key':'123','draft_id':7})
    one.call = AsyncMock(return_value={'skus':[{}]})
    assert (await one.collect())['draft_id'] == 7
    assert one.call.call_args.args == ('/api.product.collect/detail',)


async def test_capacity_blocks_before_draft_write(tmp_path):
    one = collector(tmp_path)
    capacity.observe(one.db, capacity.account(one.c), {'used':1000,'limit':1000}, time.time())
    one.find_favorite = AsyncMock(return_value={'id':7,'sku':'123'})
    one.call = AsyncMock(side_effect=AssertionError('no write'))
    with pytest.raises(Pending, match='capacity admission'): await one.collect()
    one.call.assert_not_called()
    assert one.load()[0] == 'claimed'


def test_lifecycle_survives_stage_cycle_without_resetting_approval():
    body = {'requested_at': 1, 'listing_control_id':'intent', 'missing_fields':['attributes'], 'approved_at':7}
    lifecycle.track(body, 'publishing', 'needs_fields', 100)
    lifecycle.track(body, 'needs_fields', 'queued', 200)
    lifecycle.track(body, 'queued', 'publishing', 300)
    lifecycle.track(body, 'publishing', 'needs_fields', 400)
    lifecycle.track(body, 'needs_fields', 'queued', 500)
    lifecycle.track(body, 'publishing', 'needs_fields', 600)
    assert body['approved_at'] == 7
    assert body['lifecycle']['last_progress_at'] == 100
    assert lifecycle.reason(body, 600, {'enabled':True,'max_reentries':3,'max_stall_seconds':7200}) == 'repeated_repair_stage_entries'
    body['listing_control_id'] = 'new-explicit-intent'
    lifecycle.track(body, 'queued', 'needs_fields', 700)
    assert body['lifecycle']['repair_entries'] == 1


def test_alert_dedup_recovery_and_severity():
    alert = {'code':'low','level':'critical'}
    active, events = transition({}, [alert], 1)
    assert len(events) == 1 and events[0]['transition'] == 'opened'
    assert transition(active, [alert|{'count':1}], 2)[1] == []
    assert transition(active, [], 3)[1][0]['transition'] == 'resolved'
    assert transition(active, [alert|{'level':'warning'}], 4)[1][0]['transition'] == 'severity_changed'


@pytest.mark.parametrize('bad', [None, 'owner', 'store', 'sku', 'seller', 'offer', 'unverified'])
def test_rotated_credential_cleanup_uses_bound_publication_snapshot_only(tmp_path, bad):
    db, owner, item = setup(tmp_path)
    context = {'config':{'shop_id':'4'},'credentials':{'erp_token':'rotated'}}
    record = {'owner':owner,'store_id':'s','sku':'123','seller':'2','offer_id':'offer','verified':True,
              'snapshot':item['snapshot'],'started_at':time.time()-7200}
    if bad == 'unverified': record['verified'] = False
    elif bad: record[{'store':'store_id','offer':'offer_id'}.get(bad,bad)] = 'wrong'
    with db.connect() as c:
        c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',
                  ('s',owner,'test','maozi',json.dumps(context['config']),db.seal(context['credentials'])))
        c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,'123','2','s',0,'test'))
        c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?)',(owner,'123','2',json.dumps(record)))
    result = cleanup.candidates(db, owner, cleanup.config(db))
    assert bool(result) == (bad is None)


async def test_cleanup_zero_yield_backoff_survives_new_tick(tmp_path, monkeypatch):
    db, owner, item = setup(tmp_path)
    monkeypatch.setattr(cleanup, 'candidates', lambda *args: {'account':[item]})
    clean = AsyncMock(return_value={'state':'no_safe_candidates','deleted':0})
    monkeypatch.setattr(cleanup, 'clean_account', clean)
    await cleanup.tick(db)
    await cleanup.tick(db)
    assert clean.await_count == 1


@pytest.mark.parametrize('mismatch', [False, True])
def test_cleanup_uses_only_exact_draft_reference_from_original_publication(tmp_path, mismatch):
    db, owner, item = setup(tmp_path)
    old_context = {'owner':owner,'candidate':{'source_key':'123'},
                   'store':{'config':{'shop_id':'4'},'credentials':{'erp_token':'old-token'}}}
    SourceCollector(db, old_context).save('ready', item['snapshot'])
    record = {'owner':owner,'store_id':'s','sku':'123','seller':'2','offer_id':'offer','verified':True,
              'started_at':time.time()-7200, 'review':{'candidate':{'origin':{'collection_evidence':{
                  'last_repair':{'steps':[{'source':'maozi-erp-draft','draft_id':9 if mismatch else 8}]}}}}}}
    with db.connect() as c:
        c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',
                  ('s',owner,'test','maozi','{"shop_id":"4"}',db.seal({'erp_token':'new-token'})))
        c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,'123','2','s',0,'test'))
        c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?)',(owner,'123','2',json.dumps(record)))
    assert bool(cleanup.candidates(db,owner,cleanup.config(db))) is not mismatch


def test_service_entry_waits_for_existing_supervisor_lock(tmp_path, monkeypatch):
    import fcntl
    from flowhub import service_entry
    (tmp_path/'flowhub.sqlite3').touch()
    monkeypatch.setenv('FLOWHUB_DATA',str(tmp_path))
    held=(tmp_path/'supervisor.lock').open('a');fcntl.flock(held,fcntl.LOCK_EX)
    waited=[]
    def release(seconds):
        waited.append(seconds);held.close()
    class Executed(Exception):pass
    def execute(path,args):
        assert args[-2:]==['flowhub.control','_supervise']
        raise Executed()
    monkeypatch.setattr(service_entry.time,'sleep',release)
    monkeypatch.setattr(service_entry.os,'execv',execute)
    with pytest.raises(Executed):service_entry.main()
    assert waited==[10]


def test_monitor_holds_low_output_until_recovery_and_respects_pause(tmp_path, monkeypatch):
    from flowhub import stability_monitor as monitor
    output=tmp_path/'monitor';output.mkdir()
    monitor.atomic(output/'alerts.json',{'active':{'low_output_with_repair_backlog:':{'level':'critical','code':'low_output_with_repair_backlog'}}})
    report={'alerts':[], 'captured_at':'test', 'paused':{'publication':0}, 'queue':{'needs_fields':20},
            'first_stock_verified':{'rolling_minutes':{'30':4,'60':12}}, 'health':[], 'last_collection_capacity_observation':None}
    monkeypatch.setattr(monitor,'audit',lambda data:json.loads(json.dumps(report)))
    assert monitor.sample(tmp_path,output)==[]
    report['first_stock_verified']['rolling_minutes']['60']=15
    assert monitor.sample(tmp_path,output)[0]['transition']=='resolved'
    report['paused']['publication']=1
    report['alerts']=[{'level':'critical','code':'low_output_with_repair_backlog'}]
    assert monitor.sample(tmp_path,output)==[]


async def test_cleanup_scan_budget_rotates_without_starvation(tmp_path,monkeypatch):
    db,owner,item=setup(tmp_path)
    items=[dict(item,sku=str(i),snapshot={'draft_id':i,'source_key':str(i)}) for i in range(10,16)]
    class Remote:
        async def call(self,path,**kwargs):
            assert path.endswith('/lists')
            return {'used':1000,'limit':1000,'total':6,'data':[{'id':i,'goods_id':str(i),'collect_from':'ozon'} for i in range(10,16)]}
    visited=[]
    async def unavailable(db,owner,item,client):visited.append(item['sku']);return None
    monkeypatch.setattr(cleanup,'sold_observation',unavailable)
    policy=cleanup.config(db)|{'scan_limit':2}
    for _ in range(3):
        result=await cleanup.clean_account(db,owner,'a',items,policy,Remote())
        assert result['examined']==2 and result['deleted']==0
    assert visited==['10','11','12','13','14','15']
