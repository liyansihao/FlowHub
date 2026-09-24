import json,time
import pytest
from flowhub.db import Database
from flowhub.browser_source import BrowserSource
from flowhub.other_sellers import schema,parse_offers,ingest,prepare_samples,replenish
from flowhub.source_library import SourceLibrary


def page(ids=('10','20'),count=2,sku='123'):
    import html
    state=html.escape(json.dumps({'count':str(count),'modalLink':'/modal/otherOffersFromSellers?product_id='+sku}))
    return '<div id="state-webBestSeller-x" data-state="'+state+'"></div><a href="/seller/999/">recommendation</a><div data-widget="webSellerList">'+''.join('<div><a href="/seller/'+i+'/">seller</a><a href="/seller/'+i+'/">logo</a></div>' for i in ids)+'</div><a href="/seller/888/">footer</a>'


def test_modal_identity_scope_and_explicit_count():
    assert parse_offers(page(),'123')['sellers']==['10','20']
    assert parse_offers(page(),'123')['complete']
    assert not parse_offers(page(ids=('10',)),'123')['complete']
    with pytest.raises(ValueError,match='seed_mismatch'):parse_offers(page(),'124')


def test_discovery_replay_and_samples_never_copy_seed_sku_as_shop_product(tmp_path):
    db=Database(tmp_path);BrowserSource(db);schema(db)
    with db.connect() as c:c.execute('INSERT INTO source_discovery_seeds VALUES(?,?,?,0,0,?,?)',('o','123','queued','{}',time.time()))
    assert ingest(db,'o','123',page(),'file')['new_sellers']==2
    assert ingest(db,'o','123',page(),'file')['state']=='replay'
    assert prepare_samples(db,'o')==2
    assert prepare_samples(db,'o')==0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM sourcing_products').fetchone()[0]==0
        assert {r[0] for r in c.execute('SELECT state FROM source_discovered_stores')}=={'pending_sample'}
        assert {r[0] for r in c.execute('SELECT state FROM browser_source_scans')}=={'ready'}


def test_qualified_source_can_seed_before_publication_or_order(tmp_path):
    db=Database(tmp_path);schema(db);lib=SourceLibrary(db)
    p={'sku':'123','seller_id':'10','title':'t','image':'https://example.com/i','category_id':'1',
       'sales_schema':'FBS','collected_at':time.time(),'coverage':'storefront-page'}
    lib.put('o',p,{'channel':'test'})
    assert replenish(db,'o')==1
    assert replenish(db,'o')==0
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM sourcing_seeds').fetchone()[0]==0


@pytest.mark.asyncio
async def test_manual_sample_pause_is_not_resumed(tmp_path):
    from flowhub.other_sellers import sample_one
    db=Database(tmp_path);s=BrowserSource(db);schema(db)
    with db.connect() as c:c.execute('INSERT INTO source_discovery_seeds VALUES(?,?,?,0,0,?,?)',('o','123','queued','{}',time.time()))
    ingest(db,'o','123',page(ids=('10',),count=1),'file');prepare_samples(db,'o')
    s.control('o','other-seller-10','10','pause')
    assert (await sample_one(db,'o',{}))['state']=='no_due_sample'
    assert s.next_request('o','other-seller-10','10')['state']=='paused'


@pytest.mark.asyncio
async def test_sample_due_query_does_not_block_event_loop(tmp_path,monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from flowhub.other_sellers import sample_one
    db=Database(tmp_path);schema(db);prepare_samples(db,'o')
    original=db.connect
    @contextmanager
    def slow_connect():
        time.sleep(.35)
        with original() as connection:yield connection
    monkeypatch.setattr(db,'connect',slow_connect)
    task=asyncio.create_task(sample_one(db,'o',{}))
    began=time.monotonic()
    await asyncio.sleep(.02)
    assert time.monotonic()-began<.2
    assert (await task)['state']=='no_due_sample'


def test_pending_second_generation_requires_explicit_exploration_policy(tmp_path):
    db=Database(tmp_path);schema(db);lib=SourceLibrary(db)
    p={'sku':'123','seller_id':'10','title':'t','image':'https://example.com/i',
       'collected_at':time.time(),'coverage':'storefront-page',
       'source_relation':{'seller_id':'10','root_seeds':[{'sku':'9','channel':'other-seller-discovery'}]}}
    lib.put('o',p,{'channel':'test'})
    assert replenish(db,'o')==0
    with db.connect() as c:c.execute('DELETE FROM source_discovery_checks')
    assert replenish(db,'o',explore_pending=True)==1
    with db.connect() as c:
        body=json.loads(c.execute('SELECT body FROM source_discovery_seeds').fetchone()[0])
        assert body['exploration_only'] and body['assessment']['state']=='needs_review'


@pytest.mark.asyncio
@pytest.mark.parametrize('success',[False,True])
async def test_discovery_persistence_waits_off_loop(tmp_path,monkeypatch,success):
    import asyncio,sqlite3,threading
    from flowhub.other_sellers import discover_one
    db=Database(tmp_path);BrowserSource(db);schema(db)
    with db.connect() as c:
        c.execute('INSERT INTO source_discovery_seeds VALUES(?,?,?,0,0,?,?)',('o','123','queued','{}',time.time()))
    artifact=tmp_path/'offers.html';artifact.write_text(page())
    blocker=sqlite3.connect(db.path,check_same_thread=False);started=asyncio.Event();timers=[]
    class Process:
        returncode=0 if success else 1
        async def communicate(self):
            blocker.execute('BEGIN IMMEDIATE')
            timer=threading.Timer(.4,blocker.commit);timer.start();timers.append(timer)
            started.set()
            return (json.dumps({'artifact':str(artifact)} if success else {'error':'access_challenge'}).encode(),b'')
    async def spawn(*args,**kwargs):return Process()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    try:
        task=asyncio.create_task(discover_one(db,'o',{'profile':'test'}))
        await started.wait();began=time.monotonic()
        await asyncio.sleep(.02)
        assert time.monotonic()-began<.2
        result=await task
        assert result['state']==('complete' if success else 'blocked')
        with db.connect() as c:
            assert c.execute('SELECT state FROM source_discovery_seeds').fetchone()[0]==result['state']
            assert c.execute('SELECT count(*) FROM source_discovery_failures').fetchone()[0]==(0 if success else 1)
    finally:
        for timer in timers:timer.join()
        blocker.close()


def test_unchanged_source_assessments_do_not_take_a_write_transaction(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from flowhub.other_sellers import promote_qualified
    db=Database(tmp_path);BrowserSource(db);schema(db)
    with db.connect() as c:c.execute('INSERT INTO source_discovery_seeds VALUES(?,?,?,0,0,?,?)',('o','123','queued','{}',time.time()))
    ingest(db,'o','123',page(ids=('10',),count=1),'file');prepare_samples(db,'o')
    with db.connect() as c:c.execute("UPDATE source_discovered_stores SET state='awaiting_qualified_product'")
    assert promote_qualified(db,'o')==0
    original=db.connect;changes=[]
    @contextmanager
    def observed():
        with original() as c:
            before=c.total_changes
            yield c
            changes.append(c.total_changes-before)
    monkeypatch.setattr(db,'connect',observed)
    assert promote_qualified(db,'o')==0
    assert sum(changes)==0
    SourceLibrary(db).put('o',{'sku':'123','seller_id':'10','title':'incomplete','collected_at':time.time()},{'channel':'test'})
    changes.clear()
    assert promote_qualified(db,'o')==0
    assert sum(changes)==1
    with db.connect() as c:
        row=c.execute('SELECT state,body FROM source_discovered_stores').fetchone()
        assert row['state']=='awaiting_qualified_product'
        assert json.loads(row['body'])['source_assessments'][0]['sku']=='123'
    changes.clear()
    assert promote_qualified(db,'o')==0 and sum(changes)==0
