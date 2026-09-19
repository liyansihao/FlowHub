import asyncio
import json
import time
import pytest
from flowhub import plugin_pipeline as pipeline
from flowhub.pipeline_modules import isolation
from flowhub.pipeline_modules.repair import PriceRepairModule
from tests.test_maozi_field_repair import setup


def configured(tmp_path,publication=True):
    db,owner=setup(tmp_path);pipeline.schema(db)
    (tmp_path/'repair-workflow.json').write_text(json.dumps({'enabled':True}))
    body={'requested_at':100,'official_dossier_pending':publication,'repair_retry':{'total_attempts':10}}
    with db.connect() as c:
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'1','2','needs_fields',json.dumps(body),0,0))
    return db,owner


@pytest.mark.asyncio
async def test_stage_restart_does_not_repeat_finished_source_or_publish(tmp_path,monkeypatch):
    db,owner=configured(tmp_path);calls=[]
    async def stage(self,db,owner,sku,seller,**kwargs):
        name=kwargs['stage'];calls.append(name)
        if name=='facts':return {'state':'progress','reason':'facts_done','next_stage':'source'}
        if name=='source':return {'state':'progress','reason':'source_done','next_stage':'validate'}
        return {'state':'ready','reason':'complete_dossier'}
    monkeypatch.setattr(PriceRepairModule,'run_stage',stage)
    for _ in range(3):
        assert await pipeline.tick(db,lane='seed_repair',repair_kind='publication')
    with db.connect() as c:
        row=c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
        assert row['state']=='queued'
        assert json.loads(row['body'])['repair_workflow']['state']=='ready'
        assert c.execute('SELECT count(*) FROM repair_workflow_events').fetchone()[0]==3
    assert calls==['facts','source','validate']


@pytest.mark.asyncio
async def test_dependency_failure_keeps_stage_and_old_failures_do_not_quarantine(tmp_path,monkeypatch):
    db,owner=configured(tmp_path)
    async def stage(self,*args,**kwargs):raise TimeoutError()
    monkeypatch.setattr(PriceRepairModule,'run_stage',stage)
    await pipeline.tick(db,lane='seed_repair',repair_kind='publication')
    with db.connect() as c:
        row=c.execute('SELECT * FROM plugin_pipeline').fetchone();body=json.loads(row['body'])
    assert row['state']=='needs_fields'
    assert body['repair_workflow']['stage']=='facts'
    assert body['repair_workflow']['failure_class']=='network'
    assert isolation.classify('needs_fields',body,time.time()+10000) is None
    assert not await pipeline.tick(db,lane='seed_repair',repair_kind='publication')


@pytest.mark.asyncio
async def test_definite_missing_attribute_goes_to_explicit_manual_repair(tmp_path,monkeypatch):
    db,owner=configured(tmp_path)
    async def stage(self,*args,**kwargs):
        if kwargs['stage']=='facts':return {'state':'progress','next_stage':'validate','reason':'cached'}
        return {'state':'waiting','reason':'missing:required_attribute_31','missing_fields':['31'],'failure_class':'missing_fields'}
    monkeypatch.setattr(PriceRepairModule,'run_stage',stage)
    await pipeline.tick(db,lane='seed_repair',repair_kind='publication')
    await pipeline.tick(db,lane='seed_repair',repair_kind='publication')
    with db.connect() as c:
        row=c.execute('SELECT * FROM plugin_pipeline').fetchone();body=json.loads(row['body'])
    assert row['state']=='needs_review' and body['repair_manual']
    assert body['repair_workflow']['missing_fields']==['31']
    assert 'repair_input_required' in body['reason']


@pytest.mark.asyncio
async def test_publication_and_valuation_have_independent_live_slots(tmp_path,monkeypatch):
    db,owner=configured(tmp_path)
    with db.connect() as c:
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'3','2','needs_fields','{}',0,0))
        c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,'3','2','test',0,'expired-publication'))
    entered=asyncio.Event();release=asyncio.Event();seen=[]
    async def stage(self,db,owner,sku,seller,**kwargs):
        seen.append((sku,kwargs['purpose']))
        if sku=='1':entered.set();await release.wait()
        return {'state':'progress','next_stage':'source','reason':'facts_done'}
    monkeypatch.setattr(PriceRepairModule,'run_stage',stage)
    first=asyncio.create_task(pipeline.tick(db,lane='seed_repair',repair_kind='publication'))
    await asyncio.wait_for(entered.wait(),2)
    assert await pipeline.tick(db,lane='seed_repair',repair_kind='valuation')
    release.set();await first
    assert seen==[('1','publication'),('3','valuation')]


@pytest.mark.asyncio
async def test_real_facts_stage_persists_before_any_source_write(tmp_path,monkeypatch):
    from flowhub.source_detail import SourceCollector
    from flowhub.maozi import MaoziPublisher
    db,owner=configured(tmp_path)
    async def forbidden(self):pytest.fail('facts stage must never create a draft')
    async def erp(self,*args,**kwargs):return {'status':{'update_sales':False},'data':{'sku':'1','sellerId':'2','avgPrice':300}}
    monkeypatch.setattr(SourceCollector,'collect',forbidden);monkeypatch.setattr(MaoziPublisher,'erp',erp)
    await pipeline.tick(db,lane='seed_repair',repair_kind='publication')
    with db.connect() as c:
        work=json.loads(c.execute('SELECT body FROM repair_workflows').fetchone()[0])
        product=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
    assert work['stage']=='source'
    assert product['plugin_detail']['monthly_sales']['average_price_rub']==300


def test_publication_backlog_does_not_lower_valuation_budget(tmp_path):
    from tests.test_continuous_admission import setup as admission_setup
    from flowhub.pipeline_modules.admission import admit_one
    db,owner=admission_setup(tmp_path)
    (tmp_path/'repair-workflow.json').write_text('{"enabled":true,"valuation_queue_limit":16}')
    with db.connect() as c:
        c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.max_inflight',12)")
        for i in range(9):c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,str(100+i),'3','needs_fields','{}',0,0))
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'200','3','needs_fields','{"official_dossier_pending":true}',0,0))
    assert admit_one(db,owner)['state']=='admitted'


@pytest.mark.asyncio
async def test_completed_source_validation_is_not_starved_by_older_source_backlog(tmp_path,monkeypatch):
    db,owner=configured(tmp_path);seen=[]
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET body=json_set(body,'$.repair_workflow.stage','source'),due=0")
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'3','2','needs_fields',json.dumps({'official_dossier_pending':True,'repair_workflow':{'stage':'validate'}}),time.time()-1,0))
        c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,'3','2','test',0,'expired-publication'))
    async def run(self,db,owner,sku,seller,**kwargs):
        seen.append(sku);return {'state':'waiting','reason':'test','failure_class':'network'}
    monkeypatch.setattr(PriceRepairModule,'run',run)
    assert await pipeline.tick(db,lane='seed_repair',repair_kind='publication')
    assert seen==['3']


@pytest.mark.asyncio
async def test_validate_lane_cannot_claim_slow_acquisition(tmp_path,monkeypatch):
    db,owner=configured(tmp_path);seen=[]
    async def run(self,db,owner,sku,seller,**kwargs):
        seen.append(sku);return {'state':'waiting','reason':'test','failure_class':'network'}
    monkeypatch.setattr(PriceRepairModule,'run',run)
    assert not await pipeline.tick(db,lane='seed_repair',repair_kind='publication',repair_stage='validate')
    assert await pipeline.tick(db,lane='seed_repair',repair_kind='publication',repair_stage='acquire')
    assert seen==['1']


@pytest.mark.asyncio
async def test_repair_journal_writer_wait_keeps_main_loop_responsive(tmp_path,monkeypatch):
    import sqlite3,threading
    from flowhub.pipeline_modules.repair_workflow import run_one
    db,owner=configured(tmp_path)
    with db.connect() as c:c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'1','2','lease',time.time()+60))
    async def stage(self,*args,**kwargs):return {'state':'ready','reason':'complete_dossier'}
    monkeypatch.setattr(PriceRepairModule,'run_stage',stage)
    blocker=sqlite3.connect(db.path,check_same_thread=False);blocker.execute('BEGIN IMMEDIATE')
    timer=threading.Timer(.4,blocker.commit);timer.start()
    try:
        began=time.monotonic();task=asyncio.create_task(run_one(PriceRepairModule(),db,owner,'1','2'))
        await asyncio.sleep(.02);assert time.monotonic()-began<.2
        assert (await task)['state']=='ready'
    finally:timer.join();blocker.close()
