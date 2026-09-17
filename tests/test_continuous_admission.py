import json,time
import pytest
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.pipeline_modules.admission import schema,admit_one
from flowhub.pipeline_modules.control import set_paused


def setup(tmp_path):
    db=Database(tmp_path);lib=SourceLibrary(db);schema(db)
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('INSERT INTO stores(id,owner,name,kind,config,secret,verified) VALUES(?,?,?,?,?,?,1)',('test',owner,'test','maozi',json.dumps({'shop_id':'1','warehouse_id':'2','watermark_id':'3'}),db.seal({})))
        policy={'run_id':'test','store_ids':['test'],'max_inflight':1,'allow_unknown':True,'retain_captured_asking_price':True}
        c.execute('INSERT INTO pipeline_campaigns VALUES(?,?,?,?)',(owner,1,json.dumps(policy),time.time()))
    for sku in ('1','2'):
        lib.put(owner,{'sku':sku,'seller_id':'3','title':'x','collected_at':100,'coverage':'storefront-page','current_price_display':'20 ¥','source_relation':{'seller_id':'3','root_seeds':[{'sku':'9','shop':'1','offer':'root'}]}},{'channel':'test'})
    return db,owner


def test_restart_dedup_backpressure_and_real_source_time_preserved(tmp_path):
    db,owner=setup(tmp_path)
    assert admit_one(db,owner)['state']=='admitted'
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='queued'")
    assert admit_one(Database(tmp_path),owner)['state']=='backpressure'
    with db.connect() as c:
        p=json.loads(c.execute("SELECT body FROM sourcing_products WHERE sku='1'").fetchone()[0])
        assert p['collected_at']==100 and p['proposed_sale_price']['reference_observed_at']==100
        assert p['proposed_sale_price']['source']=='campaign-asking-price-decision'
        c.execute("UPDATE plugin_pipeline SET state='selling'")
    assert admit_one(db,owner)['sku']=='2'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM pipeline_admissions').fetchone()[0]==2


def test_paused_and_expired_campaign_admits_nothing(tmp_path):
    db,owner=setup(tmp_path);set_paused(db,'seed',True)
    assert admit_one(db,owner)['state']=='paused'
    set_paused(db,'seed',False)
    with db.connect() as c:c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.until',1)")
    assert admit_one(db,owner)['state']=='window_closed'
    with db.connect() as c:assert c.execute('SELECT count(*) FROM plugin_pipeline').fetchone()[0]==0


@pytest.mark.asyncio
async def test_late_readback_retains_queue_and_can_finish_after_restart(tmp_path,monkeypatch):
    from flowhub import plugin_pipeline as pipeline
    db,owner=setup(tmp_path);admit_one(db,owner)
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='publishing',body=json_set(body,'$.phase','reconciling')")
    async def pending(*args):return {'phase':'manual_review','retryable_readback':True,'verified':False}
    monkeypatch.setattr(pipeline,'advance',pending)
    await pipeline.tick(db,lane='reconcile')
    with db.connect() as c:
        assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='awaiting_remote'
        c.execute('UPDATE plugin_pipeline SET due=0')
    async def sold(*args):return {'phase':'stock_verified','verified':True}
    monkeypatch.setattr(pipeline,'advance',sold)
    await pipeline.tick(Database(tmp_path),lane='reconcile_history')
    with db.connect() as c:assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='selling'


@pytest.mark.asyncio
async def test_shared_shop_reads_are_credential_scoped_and_writes_invalidate():
    import httpx
    from flowhub.pipeline_modules.transport import StepTransport
    StepTransport.shared_shops.clear();seen=[]
    async def handle(request):seen.append(request.method);return httpx.Response(200,json={'code':1,'data':[]})
    async def get(namespace,method='GET'):
        async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=StepTransport(httpx.MockTransport(handle),namespace=namespace)) as c:
            return await c.request(method,'/api.shop/lists')
    await get('a');await get('a');assert len(seen)==1
    await get('b');assert len(seen)==2
    await get('a','POST');await get('a');assert len(seen)==4


def test_procurement_revision_is_versioned_and_rejects_external_price_change(tmp_path):
    import copy,sqlite3
    from flowhub.pipeline_modules.dossier import refresh_procurement_plan,reviewed_snapshot
    detail={'contract':'maozi-plugin-sku-detail-v1','sku':'1','observed_at':time.time(),'dimensions_mm':[150,100,10],'weight_g':20,'attributes':[{'id':1}]}
    latest={'candidate':{'source_key':'1','title':'a','image':'https://example.com/a.jpg','origin':{'plugin_detail':detail}},'result':{'supplier_id':'11','purchase':4,'evidence':{'profit':{'sell_price_cny':30}}}}
    old={'offer_id':'stable','review':{'old':True},'snapshot':reviewed_snapshot(latest),'plan':{'supplier_identity':'10','purchase_price_cny':'3','sell_price_cny':'30'}}
    path=tmp_path/'journal.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE zero_stock_tests(offer_id TEXT,phase TEXT,plan TEXT,details TEXT)')
        c.execute('INSERT INTO zero_stock_tests VALUES(?,?,?,?)',('stable','reconciling',json.dumps(old['plan']),'{}'))
    db=Database(tmp_path/'db')
    with db.connect() as c:
        new=refresh_procurement_plan(c,path,old,latest)
        assert new['plan']['supplier_identity']=='11' and old['plan']['supplier_identity']=='10'
        assert new['plan_revisions'][0]['previous_plan']==old['plan']
        assert json.loads(c.execute('SELECT plan FROM publication_journal.zero_stock_tests').fetchone()[0])==new['plan']
    changed=copy.deepcopy(latest);changed['result']['evidence']['profit']['sell_price_cny']=31
    with db.connect() as c:
        with pytest.raises(ValueError,match='prepared_plan_repricing_required'):refresh_procurement_plan(c,path,new,changed)
    with sqlite3.connect(path) as c:assert json.loads(c.execute('SELECT plan FROM zero_stock_tests').fetchone()[0])['sell_price_cny']=='30'


def test_billing_dependency_pauses_new_admissions_without_losing_existing_queue(tmp_path):
    db,owner=setup(tmp_path);admit_one(db,owner)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='awaiting_dependency'")
        c.execute('INSERT INTO pipeline_capabilities VALUES(?,?,?,?)',('qwen_review','blocked',json.dumps({'reason':'Arrearage'}),time.time()))
    assert admit_one(db,owner)['state']=='awaiting_dependency'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM pipeline_admissions').fetchone()[0]==1
        c.execute("UPDATE pipeline_capabilities SET state='ready'")
    assert admit_one(db,owner)['sku']=='2'


def test_rub_asking_policy_does_not_invent_cny_conversion():
    from flowhub.pipeline_modules.admission import asking_quote
    assert asking_quote({'current_price_rub':283})=={'value':283.0,'currency':'RUB'}
    assert asking_quote({'current_price_display':r'49,71\u2009¥'})=={'value':49.71,'currency':'CNY'}


def test_remote_processing_does_not_hold_preparation_slot_but_is_bounded(tmp_path):
    db,owner=setup(tmp_path)
    admit_one(db,owner)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing',body=json_set(body,'$.phase','sync_pending')")
        c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.max_remote_pending',1)")
    assert admit_one(db,owner)['remote_pending']==1
    with db.connect() as c:c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.max_remote_pending',2)")
    assert admit_one(db,owner)['sku']=='2'


def test_readback_backoff_survives_restart_and_stock_write_is_immediate():
    from flowhub.plugin_pipeline import readback_delay
    body={}
    assert readback_delay(body,'reconciling')==60
    body=json.loads(json.dumps(body))
    assert readback_delay(body,'reconciling')==120
    assert readback_delay(body,'reconciling')==240
    assert readback_delay(body,'reconciling')==240
    assert readback_delay(body,'stock_ready')==0
    assert readback_delay(body,'stock_pending')==20
    assert readback_delay(body,'stock_verified',True)==0
    assert 'readback_schedule' not in body


def test_submission_priority_defers_remote_reads_but_not_stock_write():
    from flowhub.plugin_pipeline import readback_delay
    b={}
    assert readback_delay(b,'reconciling',submission_priority=True)==180
    assert readback_delay(b,'reconciling',submission_priority=True)==360
    assert readback_delay(b,'stock_ready',submission_priority=True)==0
    assert readback_delay(b,'stock_pending',submission_priority=True)==20


def test_acceptance_cohort_precedes_backlog_without_bypassing_bounds(tmp_path):
    db,owner=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.acceptance_skus',json('[\"2\"]'))")
    assert admit_one(db,owner)['sku']=='2'
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='queued'")
    assert admit_one(db,owner)['state']=='backpressure'
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='selling'")
    assert admit_one(db,owner)['sku']=='1'


def test_acceptance_cohort_still_excludes_blocked_skus(tmp_path):
    db,owner=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.acceptance_skus',json('[\"2\"]'))")
        c.execute('INSERT INTO blocks VALUES(?,?,?)',(owner,'2','explicit exclusion'))
    assert admit_one(db,owner)['sku']=='1'


def test_sleeping_repairs_release_capacity_but_repair_backlog_is_bounded(tmp_path):
    db,owner=setup(tmp_path);admit_one(db,owner)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='needs_fields',due=?",(time.time()+300,))
        c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.max_repair_pending',1)")
    assert admit_one(db,owner)=={'state':'backpressure','repair_pending':1}
    with db.connect() as c:c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.max_repair_pending',48)")
    assert admit_one(db,owner)['sku']=='2'


def test_running_repair_reserves_a_main_slot(tmp_path):
    db,owner=setup(tmp_path);admit_one(db,owner)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='needs_fields'")
        c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'1','3','lease',time.time()+120))
    assert admit_one(db,owner)['state']=='backpressure'


def make_ready(db,owner,sku='2'):
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products WHERE sku=?',(sku,)).fetchone()[0])
    p.update(image='https://example.com/a.jpg',url='https://www.ozon.ru/product/'+sku,
             plugin_detail={'sku':sku,'weight_g':100},
             proposed_sale_price={'value':20,'currency':'CNY','observed_at':time.time()})
    SourceLibrary(db).put(owner,p,{'channel':'test'})


def test_ready_admission_bypasses_full_repairs_but_respects_main_capacity(tmp_path):
    db,owner=setup(tmp_path);admit_one(db,owner);make_ready(db,owner)
    with db.connect() as c:c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.max_repair_pending',1)")
    assert admit_one(db,owner)['sku']=='2'
    with db.connect() as c:
        assert c.execute("SELECT state FROM plugin_pipeline WHERE sku='2'").fetchone()[0]=='queued'
        assert c.execute("SELECT count(*) FROM plugin_pipeline WHERE state='needs_fields'").fetchone()[0]==1
    assert admit_one(db,owner)['state']=='backpressure'


def test_new_facts_promote_during_backoff_without_re_admission(tmp_path):
    db,owner=setup(tmp_path);admit_one(db,owner);make_ready(db,owner,'1')
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET due=?,body=json_set(body,'$.repair_retry',json(?))",(time.time()+3600,json.dumps({'attempts':3})))
    assert admit_one(db,owner)=={'state':'promoted','sku':'1','seller':'3'}
    with db.connect() as c:
        row=c.execute('SELECT * FROM plugin_pipeline').fetchone();body=json.loads(row['body'])
        assert row['state']=='queued' and row['due']<=time.time()
        assert body['repair_history']==[{'attempts':3}] and 'repair_retry' not in body
        assert c.execute('SELECT count(*) FROM pipeline_admissions').fetchone()[0]==1


@pytest.mark.parametrize('guard',['pause','lease','review','capacity'])
def test_promotion_preserves_guards(tmp_path,guard):
    db,owner=setup(tmp_path);admit_one(db,owner);make_ready(db,owner,'1')
    with db.connect() as c:
        if guard=='lease':c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'1','3','live',time.time()+120))
        if guard=='review':
            c.execute('CREATE TABLE plugin_reviews(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
            c.execute('INSERT INTO plugin_reviews VALUES(?,?,?,?)',(owner,'1','3','{"state":"matched"}'))
        if guard=='capacity':c.execute("INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)",(owner,'99','3','queued','{}',0,0))
    if guard=='pause':set_paused(db,'seed',True)
    assert admit_one(db,owner)['state']!='promoted'
    with db.connect() as c:assert c.execute("SELECT state FROM plugin_pipeline WHERE sku='1'").fetchone()[0]=='needs_fields'


@pytest.mark.asyncio
async def test_first_repair_precedes_older_publication_retry(tmp_path,monkeypatch):
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db,owner=setup(tmp_path);admit_one(db,owner);admit_one(db,owner)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET due=0,body=json_set(body,'$.evaluation_state','matched','$.repair_retry',json('{\"attempts\":3}')) WHERE sku='1'")
        c.execute("UPDATE plugin_pipeline SET due=1 WHERE sku='2'")
    seen=[]
    async def repair(*args):seen.append(args[-2]);return {'state':'waiting','reason':'missing:weight_g'}
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    assert await pipeline.tick(db,lane='seed_repair')
    assert seen==['2']


@pytest.mark.asyncio
async def test_second_repair_cannot_take_reserved_main_slot(tmp_path,monkeypatch):
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db,owner=setup(tmp_path);admit_one(db,owner);admit_one(db,owner)
    with db.connect() as c:c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'1','3','live',time.time()+120))
    async def repair(*args):pytest.fail('active repair already reserved only slot')
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    assert not await pipeline.tick(db,lane='seed_repair')


@pytest.mark.asyncio
async def test_explicit_single_repair_runs_without_resuming_campaign(tmp_path,monkeypatch):
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db,owner=setup(tmp_path);admit_one(db,owner);admit_one(db,owner)
    set_paused(db,'seed',True);seen=[]
    async def repair(*args):seen.append(args[-2]);return {'state':'ready','reason':'valuation_inputs_ready'}
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    assert not await pipeline.tick(db,lane='seed_repair',target=(owner,'2','3'))
    assert await pipeline.tick(db,lane='seed_repair',target=(owner,'2','3'),run_paused=True)
    assert seen==['2'] and admit_one(db,owner)['state']=='paused'
    with pytest.raises(ValueError):await pipeline.tick(db,lane='seed_repair',run_paused=True)
    with pytest.raises(ValueError):await pipeline.tick(db,lane='submit',target=(owner,'2','3'),run_paused=True)
