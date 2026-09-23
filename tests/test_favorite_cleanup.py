import json,time,hashlib
from pathlib import Path
import pytest
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.pipeline_modules.admission import schema as pipeline_schema
from flowhub.pipeline_modules import favorite_cleanup as cleanup


def setup(tmp_path):
    db=Database(tmp_path);SourceLibrary(db);pipeline_schema(db);cleanup.schema(db)
    (tmp_path/'favorite-cleanup.json').write_text(json.dumps({'enabled':True}))
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('INSERT INTO pipeline_campaigns VALUES(?,?,?,?)',(owner,1,'{}',0))
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'123','2','selling',json.dumps({'offer_id':'offer'}),time.time()-7200,0))
        c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?)',(owner,'123','2',json.dumps({'offer_id':'offer','phase':'stock_verified'})))
    SourceLibrary(db).put(owner,{'sku':'123','seller_id':'2','title':'source','collected_at':100,'source_relation':{'seller_id':'2','root_seeds':[{'sku':'9','shop':'4','offer':'root'}]}},{'channel':'test'})
    return db,owner,{'owner':owner,'sku':'123','seller':'2','offer_id':'offer','context':{'store':{'config':{'shop_id':'4'},'credentials':{'erp_token':'test'}}}}


class Fake:
    def __init__(self,mode='ok',db=None):self.present=True;self.writes=0;self.mode=mode;self.db=db
    async def call(self,path,method='GET',params=None,body=None):
        if path.endswith('favorite/lists'):
            return {'used':3000 if self.present else 2999,'limit':3000,'total':int(self.present),'data':[{'id':7,'sku':'123','is_imported':1,'title':'source','sell_price':10}] if self.present else []}
        if path.endswith('online/lists'):
            return {'data':[{'shop_id':'4','offer_id':'other' if self.mode=='wrong_offer' else 'offer','online_status':'selling','stock':0 if self.mode=='no_stock' else 99}]}
        assert path=='/api.product.favorite/toggle' and method=='POST' and body['status'] is False
        assert body['productInfo']['sku']=='123'
        if self.db:
            with self.db.connect() as c:r=c.execute('SELECT * FROM favorite_cleanup_receipts').fetchone()
            assert r['state']=='intent'
            b=self.db.open(r['body']);assert Path(b['archive_path']).exists()
        self.writes+=1
        if self.mode=='unknown_present':raise TimeoutError()
        self.present=False
        if self.mode=='lost_ack':raise TimeoutError()
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['ok','lost_ack','unknown_present','no_stock','wrong_offer'])
async def test_archive_before_delete_retains_seed_graph_and_does_not_replay(tmp_path,mode):
    db,owner,item=setup(tmp_path);api=Fake(mode,db)
    with db.connect() as c:
        before=c.execute('SELECT body FROM sourcing_products').fetchone()[0]
        evidence=c.execute('SELECT count(*) FROM sourcing_evidence').fetchone()[0]
    await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    with db.connect() as c:
        r=c.execute('SELECT * FROM favorite_cleanup_receipts').fetchone()
        assert c.execute('SELECT body FROM sourcing_products').fetchone()[0]==before
        assert c.execute('SELECT count(*) FROM sourcing_evidence').fetchone()[0]==evidence
        assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='selling'
    if mode in ('no_stock','wrong_offer'):assert r is None
    else:
        b=db.open(r['body']);assert b['root_seeds'][0]['sku']=='9'
        raw=Path(b['archive_path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==b['archive_sha256']
        assert json.loads(raw)['favorite']['id']==7
        assert r['state']==('unconfirmed' if mode=='unknown_present' else 'deleted')
    await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    assert api.writes==(0 if mode in ('no_stock','wrong_offer') else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('guard',['pause','disabled','lease','missing_roots','wrong_seller','not_sold','archive_failure'])
async def test_missing_provenance_and_inflight_work_never_deleted(tmp_path,guard,monkeypatch):
    from flowhub.pipeline_modules.control import set_paused
    db,owner,item=setup(tmp_path);api=Fake()
    if guard=='pause':set_paused(db,'seed',True)
    if guard=='disabled':(tmp_path/'favorite-cleanup.json').write_text(json.dumps({'enabled':False}))
    with db.connect() as c:
        if guard=='lease':c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'123','2','live',time.time()+60))
        if guard=='not_sold':c.execute("UPDATE plugin_pipeline SET state='needs_fields'")
        if guard=='missing_roots':c.execute("UPDATE sourcing_products SET body=json_set(body,'$.source_relation.root_seeds',json('[]'))")
        if guard=='wrong_seller':c.execute("UPDATE sourcing_products SET body=json_set(body,'$.source_relation.seller_id','999')")
    if guard=='archive_failure':
        def broken(*args):raise OSError('disk full')
        monkeypatch.setattr(cleanup,'archive',broken)
        with pytest.raises(OSError):await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    else:await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    assert api.writes==0


@pytest.mark.asyncio
async def test_unstable_full_listing_never_proves_absence():
    class Moving:
        async def call(self,path,method='GET',params=None):
            return {'total':101,'data':[{'id':i} for i in range(100)] if params['page']==1 else [{'id':99}]}
    with pytest.raises(ValueError):await cleanup.listing(Moving())


@pytest.mark.asyncio
async def test_capacity_below_threshold_does_not_delete(tmp_path):
    db,owner,item=setup(tmp_path)
    class Low(Fake):
        async def call(self,*args,**kwargs):
            response=await super().call(*args,**kwargs)
            if args[0].endswith('favorite/lists'):response['used']=2000
            return response
    api=Low();result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    assert result['state']=='below_threshold' and api.writes==0


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['not_imported','duplicate_sku'])
async def test_unimported_or_ambiguous_favorites_are_retained(tmp_path,mode):
    db,owner,item=setup(tmp_path)
    class Unsafe(Fake):
        async def call(self,*args,**kwargs):
            r=await super().call(*args,**kwargs)
            if args[0].endswith('favorite/lists'):
                if mode=='not_imported':r['data'][0]['is_imported']=0
                else:r['data'].append(r['data'][0]|{'id':8});r['total']=2
            return r
    api=Unsafe();await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    assert api.writes==0


@pytest.mark.asyncio
async def test_only_reads_retry_transient_connection_failure(monkeypatch):
    async def immediate(*args):pass
    monkeypatch.setattr(cleanup.asyncio,'sleep',immediate)
    class API:
        def __init__(self):self.calls=0
        async def erp(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:raise TimeoutError()
            return {}
    client=cleanup.Client.__new__(cleanup.Client);client.api=API();client.requests=client.retries=0
    assert await client.call('/read')=={} and client.api.calls==2
    client.api=API()
    with pytest.raises(TimeoutError):await client.call('/write',method='POST')
    assert client.api.calls==1


def test_large_batch_and_short_interval_require_valid_bounds(tmp_path):
    db=Database(tmp_path)
    p=tmp_path/'favorite-cleanup.json'
    p.write_text(json.dumps({'batch_size':1000,'interval_seconds':60,'target_ratio':2/3}))
    settings=cleanup.config(db)
    assert settings['batch_size']==1000 and settings['interval_seconds']==60
    assert min(settings['batch_size'],3000-int(3000*settings['target_ratio']))==1000
    for patch in ({'batch_size':1001},{'interval_seconds':59}):
        p.write_text(json.dumps(settings|patch))
        with pytest.raises(ValueError):cleanup.config(db)


@pytest.mark.asyncio
async def test_clear_completed_favorites_do_not_stop_below_threshold(tmp_path):
    db,owner,item=setup(tmp_path)
    class Low(Fake):
        async def call(self,*args,**kwargs):
            r=await super().call(*args,**kwargs)
            if args[0].endswith('favorite/lists'):r['used']=100
            return r
    api=Low()
    result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db)|{'clear_completed':True},api)
    assert result['deleted']==1


@pytest.mark.asyncio
async def test_pause_between_pages_never_reconciles_partial_absence(tmp_path):
    db,owner,item=setup(tmp_path)
    with db.connect() as c:
        c.execute('INSERT INTO favorite_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
                  ('account','missing',owner,'999','unconfirmed',db.seal({}),0))
    class Paged(Fake):
        async def call(self,path,**kwargs):
            assert kwargs['params']['page']==1
            cleanup.control.set_paused(db,'seed',True)
            return {'total':101,'data':[{'id':i} for i in range(100)]}
    result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),Paged())
    assert result['state']=='paused'
    with db.connect() as c:
        assert c.execute('SELECT state FROM favorite_cleanup_receipts').fetchone()[0]=='unconfirmed'


@pytest.mark.asyncio
async def test_pause_stops_scan_even_when_online_items_are_ineligible(tmp_path):
    db,owner,item=setup(tmp_path)
    class Pausing(Fake):
        calls=0
        async def call(self,path,**kwargs):
            if path.endswith('online/lists'):
                self.calls+=1
                cleanup.control.set_paused(db,'seed',True)
                return {'data':[]}
            return await super().call(path,**kwargs)
    api=Pausing()
    result=await cleanup.clean_account(db,owner,'account',[item,item],cleanup.config(db),api)
    assert result['state']=='paused' and api.calls==1 and api.writes==0


@pytest.mark.asyncio
async def test_pause_after_ack_preserves_receipt_without_replaying_write(tmp_path):
    db,owner,item=setup(tmp_path)
    class Pausing(Fake):
        async def call(self,path,**kwargs):
            response=await super().call(path,**kwargs)
            if path.endswith('favorite/toggle'):cleanup.control.set_paused(db,'seed',True)
            return response
    api=Pausing(db=db)
    result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    assert result['state']=='paused' and api.writes==1
    with db.connect() as c:assert c.execute('SELECT state FROM favorite_cleanup_receipts').fetchone()[0]=='acknowledged'
    cleanup.control.set_paused(db,'seed',False)
    await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    assert api.writes==1
    with db.connect() as c:assert c.execute('SELECT state FROM favorite_cleanup_receipts').fetchone()[0]=='deleted'


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel',[False,True])
async def test_slow_archive_keeps_loop_responsive_and_drains_before_cancel(tmp_path,monkeypatch,cancel):
    import asyncio
    import threading
    db,owner,item=setup(tmp_path);api=Fake(db=db)
    entered=threading.Event();release=threading.Event();finished=threading.Event()
    original=cleanup.archive
    def slow(*args):
        entered.set()
        assert release.wait(5), 'event loop did not advance during archive'
        try:return original(*args)
        finally:finished.set()
    monkeypatch.setattr(cleanup,'archive',slow)
    task=asyncio.create_task(cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api))
    try:
        assert await asyncio.to_thread(entered.wait,3)
        assert not finished.is_set() and api.writes==0
        if cancel:
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done(), 'cancellation released work before archive drained'
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):await task
            assert finished.is_set() and api.writes==0
            with db.connect() as c:
                r=c.execute('SELECT * FROM favorite_cleanup_receipts').fetchone()
                assert r['state']=='intent'
                assert Path(db.open(r['body'])['archive_path']).exists()
            await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
            assert api.writes==0
        else:
            result=await task
            assert result['deleted']==1 and api.writes==1
    finally:
        release.set()
        if not task.done():await task


def test_evidence_product_index_preserves_all_archive_evidence(tmp_path):
    db,owner,item=setup(tmp_path)
    with db.connect() as c:
        c.executemany('INSERT INTO sourcing_evidence VALUES(?,?,?,?,?)',
            [(owner,str(i),str(123 if i%10==0 else 999),'{}',i) for i in range(100)])
        expected=[dict(r) for r in c.execute('SELECT * FROM sourcing_evidence WHERE owner=? AND sku=? ORDER BY hash',(owner,'123'))]
        plan=' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM sourcing_evidence WHERE owner=? AND sku=? ORDER BY hash',(owner,'123')))
        assert 'sourcing_evidence_product' in plan and 'sku=?' in plan
        backup=cleanup.archive(db,c,item,{'id':7,'is_imported':1},{})
    assert backup['evidence']==expected

@pytest.mark.asyncio
async def test_unrelated_favorite_churn_does_not_abort_exact_cleanup(tmp_path):
    db,owner,item=setup(tmp_path)
    class Churn(Fake):
        calls=0
        async def call(self,path,**kwargs):
            r=await super().call(path,**kwargs)
            if path.endswith('favorite/lists'):
                assert kwargs['params']['sku']=='123'
                self.calls+=1;r['used']=3000-self.calls
            return r
    api=Churn(db=db)
    result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db)|{'clear_completed':True},api)
    assert result['deleted']==1 and api.writes==1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['ignored_filter','truncated','duplicate_identity'])
async def test_exact_query_never_proves_absence_from_invalid_results(bad):
    class Bad:
        async def call(self,*args,**kwargs):
            data=[{'id':7,'sku':'123'}];total=1
            if bad=='ignored_filter':data[0]['sku']='456'
            if bad=='truncated':total=2
            if bad=='duplicate_identity':data=data*2;total=2
            return {'total':total,'data':data}
    with pytest.raises(ValueError):await cleanup.exact_favorites(Bad(),'123')

@pytest.mark.asyncio
async def test_fair_rotation_survives_new_database_handle(tmp_path):
    db,owner,item=setup(tmp_path)
    items=[item|{'sku':str(i)} for i in range(100,140)]
    class Absent:
        def __init__(self):self.seen=[]
        async def call(self,path,**kw):
            self.seen.append(kw['params']['sku'])
            return {'total':0,'data':[],'used':3000,'limit':3000}
    api=Absent();settings=cleanup.config(db)|{'max_checks_per_cycle':10}
    for _ in range(4):
        await cleanup.clean_account(Database(tmp_path),owner,'account',items,settings,api)
    assert len(api.seen)==40 and len(set(api.seen))==40
    assert set(api.seen)=={x['sku'] for x in items}

@pytest.mark.asyncio
async def test_unconfirmed_receipt_reconciled_without_candidate(tmp_path):
    db,owner,item=setup(tmp_path);api=Fake('unknown_present',db)
    await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    api.present=False
    await cleanup.clean_account(db,owner,'account',[],cleanup.config(db),api)
    assert api.writes==1
    with db.connect() as c:assert c.execute('SELECT state FROM favorite_cleanup_receipts').fetchone()[0]=='deleted'

@pytest.mark.asyncio
async def test_remote_item_failure_does_not_block_next_product(tmp_path):
    import httpx
    db,owner,item=setup(tmp_path)
    class Partial(Fake):
        async def call(self,path,method='GET',params=None,body=None):
            if params and params.get('sku')=='100':raise httpx.ConnectTimeout('offline')
            return await super().call(path,method,params,body)
    api=Partial(db=db)
    result=await cleanup.clean_account(db,owner,'account',[item|{'sku':'100'},item],cleanup.config(db),api)
    assert result['deleted']==1 and result['reasons']['remote_ConnectTimeout']==1
    assert api.writes==1

@pytest.mark.asyncio
async def test_hanging_transport_has_total_deadline_and_single_write():
    import asyncio
    class Hanging:
        def __init__(self):self.calls=0
        async def erp(self,*a,**kw):
            self.calls+=1
            await asyncio.Event().wait()
    client=cleanup.Client.__new__(cleanup.Client)
    client.api=Hanging();client.requests=client.retries=0;client.request_seconds=.02
    started=time.monotonic()
    with pytest.raises(TimeoutError):await client.call('/test',method='POST')
    assert time.monotonic()-started<.5 and client.api.calls==1

@pytest.mark.asyncio
async def test_cycle_deadline_retains_unknown_intent_without_replaying(tmp_path):
    import asyncio
    db,owner,item=setup(tmp_path)
    class HangingWrite(Fake):
        async def call(self,path,method='GET',params=None,body=None):
            if method=='POST':
                self.writes+=1
                await asyncio.Event().wait()
            return await super().call(path,method,params,body)
    api=HangingWrite(db=db);settings=cleanup.config(db)|{'cycle_seconds':.05}
    result=await cleanup.clean_account(db,owner,'account',[item],settings,api)
    assert result['reasons']['remote_TimeoutError']==1
    with db.connect() as c:assert c.execute('SELECT state FROM favorite_cleanup_receipts').fetchone()[0]=='intent'
    await cleanup.clean_account(db,owner,'account',[item],settings,api)
    assert api.writes==1

@pytest.mark.asyncio
async def test_historically_cleaned_skus_do_not_delay_new_completed_work(tmp_path):
    db,owner,item=setup(tmp_path)
    with db.connect() as c:
        c.execute('INSERT INTO favorite_cleanup_receipts VALUES(?,?,?,?,?,?,?)',('account','42',owner,'old','deleted',db.seal({}),time.time()-1000))
    work,count=cleanup.scheduled_work(db,'account',[item|{'sku':'old'},item],[],cleanup.config(db)|{'max_checks_per_cycle':1})
    assert count==2 and work[0]['item']['sku']=='123'
    cleanup.checked(db,'account','sku:123','favorite_absent',300)
    work,count=cleanup.scheduled_work(db,'account',[item|{'sku':'old'},item],[],cleanup.config(db)|{'max_checks_per_cycle':1})
    assert work[0]['item']['sku']=='old'  # History never permanently removes work.

@pytest.mark.asyncio
async def test_pending_receipt_backlog_cannot_occupy_all_cleanup_slots(tmp_path):
    db,owner,item=setup(tmp_path)
    prior=[{'favorite_id':str(i),'sku':str(i)} for i in range(85)]
    work,count=cleanup.scheduled_work(db,'account',[item],prior,cleanup.config(db)|{'max_checks_per_cycle':4})
    assert count==86 and work[0]['kind']=='candidate'
    assert sum(w['kind']=='receipt' for w in work)==3
    more=[item|{'sku':str(200+i)} for i in range(20)]
    work,_=cleanup.scheduled_work(db,'account',more,prior,cleanup.config(db)|{'max_checks_per_cycle':8})
    assert [w['kind'] for w in work]==['candidate','candidate','candidate','receipt']*2

@pytest.mark.asyncio
@pytest.mark.parametrize('guard',['ok','not_verified','wrong_backend','pending','not_selling','zero_stock','wrong_offer','lost_ack'])
async def test_official_completed_favorite_does_not_require_erp_import_flag(tmp_path,guard):
    db,owner,item=setup(tmp_path)
    with db.connect() as c:
        publication={'offer_id':'offer','backend':'official','phase':'stock_verified','verified':True}
        if guard=='not_verified':publication['verified']=False
        if guard=='wrong_backend':publication['backend']='unknown'
        if guard=='pending':publication['phase']='submitting'
        c.execute('UPDATE plugin_publications SET body=?',(json.dumps(publication),))
    class OfficialFavorite(Fake):
        async def call(self,path,method='GET',params=None,body=None):
            response=await super().call(path,method,params,body)
            if path.endswith('favorite/lists') and response['data']:response['data'][0]['is_imported']=0
            if path.endswith('online/lists') and guard=='not_selling':response['data'][0]['online_status']='not_selling'
            return response
    api=OfficialFavorite({'zero_stock':'no_stock','wrong_offer':'wrong_offer','lost_ack':'lost_ack'}.get(guard,'ok'),db)
    result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    assert api.writes==(1 if guard in ('ok','lost_ack') else 0)
    if api.writes:
        assert result['deleted']==1
        with db.connect() as c:r=c.execute('SELECT * FROM favorite_cleanup_receipts').fetchone()
        archived=db.open(r['body'])
        assert archived['publication']['backend']=='official'
        assert archived['favorite']['is_imported']==0
        assert archived['online']['online_status']=='selling'
        assert r['state']=='deleted'
        await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
        assert api.writes==1
