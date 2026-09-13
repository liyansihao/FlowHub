import json
import pytest
from flowhub.db import Database
from flowhub.browser_source import BrowserSource
from flowhub.pipeline_modules import source_loop as loop
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
