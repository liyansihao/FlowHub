import asyncio
import json
import pytest
from flowhub.db import Database
from flowhub.browser_source import BrowserSource
from flowhub.pipeline_modules import source_loop as loop
from flowhub.pipeline_modules.source_recovery import rearm
from flowhub.pipeline_modules.control import set_paused
from test_storefront import packet


def setup(tmp_path):
    db=Database(tmp_path);loop.schema(db)
    with db.connect() as c:
        c.execute('INSERT INTO sourcing_seeds VALUES(?,?,?,?,?,0,1,?)',('a','shop','offer','123',json.dumps({'seller_id':'12'}),100))
    return db


def test_discovery_idempotent_manual_pause_and_fair_rotation(tmp_path):
    db=setup(tmp_path);assert loop.sync_stores(db,'a','scan',100)==1
    s=BrowserSource(db);s.control('a','scan','12','pause')
    assert loop.sync_stores(db,'a','scan',101)==0
    assert loop.choose(db,'a',101) is None
    s.control('a','scan','12','resume')
    assert loop.choose(db,'a',101)['seller']=='12'
    with db.connect() as c:c.execute('INSERT INTO sourcing_seeds VALUES(?,?,?,?,?,0,1,?)',('a','shop','offer2','124',json.dumps({'seller_id':'13'}),100))
    assert loop.sync_stores(db,'a','scan',102)==1
    task=loop.choose(db,'a',102);loop.finish(db,task,103)
    assert loop.choose(db,'a',120)['seller']=='13'


@pytest.mark.asyncio
async def test_due_primary_store_runs_after_other_seller_sample(tmp_path,monkeypatch):
    from flowhub import other_sellers
    db=setup(tmp_path)
    calls=[]
    monkeypatch.setattr(other_sellers,'prepare_samples',lambda *args:None)
    monkeypatch.setattr(other_sellers,'promote_qualified',lambda *args:None)
    monkeypatch.setattr(other_sellers,'replenish',lambda *args,**kwargs:None)
    async def discover(*args):return {'state':'no_due_discovery'}
    async def sample(*args):
        calls.append('sample')
        return {'state':'retry_wait','seller':'99'}
    async def collect(db,task,config):
        calls.append('primary')
        assert task['seller']=='12'
        return {'state':'ready','seller':'12'}
    monkeypatch.setattr(other_sellers,'discover_one',discover)
    monkeypatch.setattr(other_sellers,'sample_one',sample)
    monkeypatch.setattr(loop,'collect',collect)
    result=await loop.tick(db,{'enabled':True,'owner':'a','run_id':'scan',
                               'other_sellers_enabled':True})
    assert calls==['sample','primary']
    assert result['state']=='ready'
    assert result['sample_state']=='retry_wait'
    assert result['sample_seller']=='99'


def test_network_retries_bounded_challenge_isolated_manual_retry(tmp_path):
    db=setup(tmp_path);loop.sync_stores(db,'a','scan',100);s=BrowserSource(db)
    task=loop.choose(db,'a',100);s.fail('a','scan','12','browser_navigation_TimeoutError')
    assert loop.finish(db,task,101)['state']=='retry_wait'
    assert loop.choose(db,'a',102) is None
    task=loop.choose(db,'a',1000);s.control('a','scan','12','retry')
    s.fail('a','scan','12','browser_access_challenge')
    assert loop.finish(db,task,1001)['state']=='blocked'
    assert loop.choose(db,'a',5000) is None
    s.control('a','scan','12','retry')
    assert loop.choose(db,'a',5000)['seller']=='12'


def test_operator_rearms_only_proven_route_failures_without_resetting_cursor(tmp_path):
    db=setup(tmp_path)
    with db.connect() as c:
        for seller in ('13','14'):
            c.execute('INSERT INTO sourcing_seeds VALUES(?,?,?,?,?,0,1,?)',
                      ('a','shop','offer'+seller,seller,json.dumps({'seller_id':seller}),100))
    loop.sync_stores(db,'a','scan',100)
    service=BrowserSource(db)
    for seller,reason in [('12','browser_access_challenge'),
                          ('13','browser_navigation_TimeoutError'),
                          ('14','cursor_mismatch')]:
        with db.connect() as c:
            c.execute('UPDATE browser_source_scans SET page=3,next_url=? WHERE seller=?',
                      (f'https://www.ozon.ru/seller/{seller}/products/?page=3',seller))
        service.fail('a','scan',seller,reason)
        task=loop.choose(db,'a',100)
        assert task['seller']==seller
        loop.finish(db,task,101)
    with db.connect() as c:
        c.execute("UPDATE source_loop_stores SET last_state='retry_exhausted' WHERE seller='13'")
        before=[tuple(r) for r in c.execute('SELECT seller,page,next_url FROM browser_source_scans ORDER BY seller')]
    with pytest.raises(ValueError,match='source_recovery_limit_out_of_range'):
        rearm(db,'a','scan',limit=11,now=200)
    with pytest.raises(ValueError,match='invalid_source_recovery_seller'):
        rearm(db,'a','scan',seller='not-a-seller',now=200)
    first=rearm(db,'a','scan',limit=1,seller='13',now=200)
    assert len(first)==1 and first[0]['seller']=='13'
    assert loop.choose(db,'a',200)['seller']=='13'
    second=rearm(db,'a','scan',limit=10,now=201)
    assert [r['seller'] for r in second]==['12']
    with db.connect() as c:
        after=[tuple(r) for r in c.execute('SELECT seller,page,next_url FROM browser_source_scans ORDER BY seller')]
        assert after==before
        assert c.execute("SELECT state FROM browser_source_scans WHERE seller='14'").fetchone()[0]=='blocked'
        assert c.execute('SELECT COUNT(*) FROM browser_source_pages').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM browser_source_failures').fetchone()[0]==3


@pytest.mark.asyncio
async def test_exact_direct_seed_identity_and_new_store(tmp_path):
    db=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE sourcing_seeds SET body='{}'")
        c.execute('INSERT INTO sourcing_settings VALUES(?,1,?,?)',('a','{}',db.seal({'erp_token':'test'})))
    async def wrong(*args):return {'data':{'sku':'999','sellerId':13}}
    assert (await loop.resolve_one(db,'a',wrong,100))['reason']=='seed_identity_mismatch'
    async def good(*args):return {'data':{'sku':'123','sellerId':13}}
    assert (await loop.resolve_one(db,'a',good,30000))['seller']=='13'
    assert loop.sync_stores(db,'a','scan',30001)==1
    assert loop.choose(db,'a',30001)['seller']=='13'


def test_global_pause_rejects_inflight_commit(tmp_path):
    db=setup(tmp_path);loop.sync_stores(db,'a','scan');s=BrowserSource(db)
    url=s.next_request('a','scan','12')['url'];set_paused(db,'seed',True)
    assert s.next_request('a','scan','12')['state']=='paused'
    with pytest.raises(Exception,match='task_paused'):s.ingest('a','scan','12',url,packet(),'a')
    set_paused(db,'seed',False)
    assert s.next_request('a','scan','12')['page']==1


def test_verified_publication_promotes_exact_root_once(tmp_path):
    db=setup(tmp_path)
    with db.connect() as c:
        c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL)')
        p={'verified':True,'sku':'456','seller':'14','offer_id':'exact-offer','product':{'shop_id':'42','sku':'999'}}
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?,?)',('a','456','14',json.dumps(p),100))
    loop.sync_stores(db,'a','scan',101);loop.sync_stores(db,'a','scan',102)
    with db.connect() as c:
        rows=c.execute("SELECT sku,body FROM sourcing_seeds WHERE offer='exact-offer'").fetchall()
        assert len(rows)==1 and rows[0]['sku']=='456'
        assert json.loads(rows[0]['body'])['seller_id']=='14'


def test_fresh_and_old_admission_alternate(tmp_path):
    from test_continuous_admission import setup as admission_setup
    from flowhub.pipeline_modules.admission import admit_one
    db,owner=admission_setup(tmp_path)
    with db.connect() as c:c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.mix_fresh_sources',1)")
    assert admit_one(db,owner)['sku']=='2'
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='selling'")
    assert admit_one(db,owner)['sku']=='1'


@pytest.mark.asyncio
async def test_completed_store_new_generation_preserves_receipts(tmp_path,monkeypatch):
    db=setup(tmp_path);loop.sync_stores(db,'a','scan',100);s=BrowserSource(db)
    key=('a','scan','12');s.ingest(*key,s.next_request(*key)['url'],packet(continuation=False),'first')
    loop.finish(db,loop.choose(db,'a',100),101)
    assert loop.choose(db,'a',200) is None
    task=loop.choose(db,'a',90000)
    class Process:
        returncode=0
        async def communicate(self):return b'',b''
    async def spawn(*args,**kwargs):return Process()
    monkeypatch.setattr(loop.asyncio,'create_subprocess_exec',spawn)
    await loop.collect(db,task,{'profile':str(tmp_path)})
    with db.connect() as c:
        assert c.execute("SELECT state FROM browser_source_scans WHERE run_id='scan'").fetchone()[0]=='done'
        assert c.execute('SELECT count(*) FROM browser_source_pages').fetchone()[0]==1
        assert c.execute('SELECT run_id FROM source_loop_stores').fetchone()[0]!='scan'


@pytest.mark.asyncio
async def test_seed_resolution_exhaustion_survives_restart_and_allows_next_seed(tmp_path):
    db=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE sourcing_seeds SET body='{}'")
        c.execute('INSERT INTO sourcing_settings VALUES(?,1,?,?)',('a','{}',db.seal({'erp_token':'test'})))
    calls=[]
    async def failed(path,query,token):
        calls.append(query['sku'])
        return {'data':{'sku':query['sku']}}
    for attempt in range(loop.SEED_RESOLUTION_MAX_ATTEMPTS):
        result=await loop.resolve_one(db,'a',failed,100+attempt*21600)
        assert result['state']==('retry_exhausted' if attempt==3 else 'waiting')
    with db.connect() as c:
        old=dict(c.execute('SELECT * FROM source_seed_resolutions').fetchone())
        c.execute('INSERT INTO sourcing_seeds VALUES(?,?,?,?,?,0,0,?)',('a','shop','next','124','{}',100))
    db=Database(tmp_path);loop.schema(db)
    async def good(path,query,token):
        assert query['sku']=='124'
        return {'data':{'sku':'124','sellerId':13}}
    assert (await loop.resolve_one(db,'a',good,1000000))['state']=='resolved'
    assert (await loop.resolve_one(db,'a',failed,2000000))['state']=='no_due_seed'
    assert len(calls)==4
    with db.connect() as c:
        assert dict(c.execute("SELECT * FROM source_seed_resolutions WHERE sku='123'").fetchone())==old
        assert c.execute("SELECT count(*) FROM sourcing_seeds WHERE sku='123'").fetchone()[0]==1


@pytest.mark.asyncio
async def test_historical_excess_attempts_stop_without_another_remote_read(tmp_path):
    db=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE sourcing_seeds SET body='{}'")
        c.execute('INSERT INTO sourcing_settings VALUES(?,1,?,?)',('a','{}',db.seal({'erp_token':'test'})))
        c.execute('INSERT INTO source_seed_resolutions VALUES(?,?,?,?,?,?,?,?,?)',('a','123','waiting',None,'network',0,24,'{"historical":"retained"}',1))
    async def forbidden(*args):pytest.fail('exhausted seed must not call ERP')
    assert (await loop.resolve_one(db,'a',forbidden,100))['state']=='retry_exhausted'
    with db.connect() as c:
        r=c.execute('SELECT * FROM source_seed_resolutions').fetchone()
        assert r['attempts']==24 and r['reason']=='network' and r['evidence']=='{"historical":"retained"}'
    assert (await loop.resolve_one(db,'a',forbidden,1000000))['state']=='no_due_seed'


@pytest.mark.asyncio
async def test_seed_resolution_can_succeed_on_final_budgeted_attempt(tmp_path):
    db=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE sourcing_seeds SET body='{}'")
        c.execute('INSERT INTO sourcing_settings VALUES(?,1,?,?)',('a','{}',db.seal({'erp_token':'test'})))
        c.execute('INSERT INTO source_seed_resolutions VALUES(?,?,?,?,?,?,?,?,?)',('a','123','waiting',None,'network',0,3,'{}',1))
    async def good(*args):return {'data':{'sku':'123','sellerId':13}}
    assert (await loop.resolve_one(db,'a',good,100))['state']=='resolved'
    with db.connect() as c:
        assert json.loads(c.execute('SELECT body FROM sourcing_seeds').fetchone()[0])['seller_id']=='13'


@pytest.mark.asyncio
async def test_seed_resolution_write_wait_does_not_block_event_loop(tmp_path):
    db=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE sourcing_seeds SET body='{}'")
        c.execute('INSERT INTO sourcing_settings VALUES(?,1,?,?)',('a','{}',db.seal({'erp_token':'test'})))
    async def good(*args):return {'data':{'sku':'123','sellerId':13}}
    with db.connect() as writer:
        writer.execute('BEGIN IMMEDIATE')
        task=asyncio.create_task(loop.resolve_one(db,'a',good,100))
        started=asyncio.get_running_loop().time()
        await asyncio.sleep(.1)
        assert asyncio.get_running_loop().time()-started<.5
        assert not task.done()
        writer.rollback()
    assert (await asyncio.wait_for(task,2))['state']=='resolved'
