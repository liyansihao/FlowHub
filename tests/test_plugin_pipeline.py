import json
import pytest
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub import plugin_pipeline as pipeline


@pytest.mark.asyncio
async def test_explicit_pipeline_persists_and_resumes_to_verified_sale(tmp_path,monkeypatch):
    db=Database(tmp_path);lib=SourceLibrary(db)
    with db.connect() as c:owner=c.execute('SELECT id FROM users').fetchone()[0]
    lib.put(owner,{'sku':'1','seller_id':'2','title':'x','collected_at':100},{'channel':'test'})
    first=pipeline.enqueue(db,owner,'1','2')
    assert pipeline.enqueue(db,owner,'1','2')==first
    calls=[]
    async def evaluate(*args):calls.append('evaluate');return {'state':'matched'}
    async def advance(*args):calls.append('publish');return {'phase':'stock_verified','verified':True,'offer_id':'exact'}
    monkeypatch.setattr(pipeline,'evaluate',evaluate);monkeypatch.setattr(pipeline,'advance',advance)
    await pipeline.tick(db)
    with db.connect() as c:c.execute('UPDATE plugin_pipeline SET due=0')
    await pipeline.tick(db)
    with db.connect() as c:r=c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
    assert r['state']=='selling' and json.loads(r['body'])['verified']
    assert calls==['evaluate','publish']
    assert not await pipeline.tick(db)


@pytest.mark.asyncio
async def test_rejected_review_never_calls_publication(tmp_path,monkeypatch):
    db=Database(tmp_path);lib=SourceLibrary(db)
    with db.connect() as c:owner=c.execute('SELECT id FROM users').fetchone()[0]
    lib.put(owner,{'sku':'1','seller_id':'2','title':'x','collected_at':100},{'channel':'test'})
    pipeline.enqueue(db,owner,'1','2')
    async def evaluate(*args):return {'state':'rejected','result':{'reason':'profit'}}
    async def advance(*args):pytest.fail('unqualified publication')
    monkeypatch.setattr(pipeline,'evaluate',evaluate);monkeypatch.setattr(pipeline,'advance',advance)
    await pipeline.tick(db)
    with db.connect() as c:assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='rejected'
    with pytest.raises(ValueError):pipeline.enqueue(db,'another-owner','1','2')

@pytest.mark.asyncio
async def test_submitted_product_is_reconciled_before_older_new_candidate(tmp_path,monkeypatch):
    db=Database(tmp_path);SourceLibrary(db);pipeline.schema(db)
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'1','2','queued','{}',0,0))
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'3','2','publishing',json.dumps({'phase':'reconciling'}),1,0))
    async def evaluate(*args):pytest.fail('new matching must not starve pending readback')
    async def advance(*args):return {'phase':'stock_verified','verified':True}
    monkeypatch.setattr(pipeline,'evaluate',evaluate);monkeypatch.setattr(pipeline,'advance',advance)
    await pipeline.tick(db)
    with db.connect() as c:
        assert c.execute("SELECT state FROM plugin_pipeline WHERE sku='3'").fetchone()[0]=='selling'
        assert c.execute("SELECT state FROM plugin_pipeline WHERE sku='1'").fetchone()[0]=='queued'


@pytest.mark.asyncio
async def test_repair_backoff_persists_and_exhausts_without_publication(tmp_path,monkeypatch):
    import time
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db=Database(tmp_path);SourceLibrary(db);pipeline.schema(db)
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'1','2','needs_fields','{}',0,0))
    async def repair(*args):return {'state':'waiting','reason':'missing:sale_price','missing_fields':['sale_price']}
    async def publish(*args):pytest.fail('repair must never publish')
    monkeypatch.setattr(PriceRepairModule,'run',repair);monkeypatch.setattr(pipeline,'advance',publish)
    for attempt in range(1,7):
        assert await pipeline.tick(Database(tmp_path),lane='seed_repair')
        with db.connect() as c:
            r=c.execute('SELECT * FROM plugin_pipeline').fetchone();b=json.loads(r['body'])
            assert b['repair_retry']['attempts']==attempt
            assert b['repair_retry']['missing_fields']==['sale_price']
            if attempt<6:
                assert r['state']=='needs_fields'
                assert r['due']-time.time()>min(3600,300*2**(attempt-1))-5
                c.execute('UPDATE plugin_pipeline SET due=0')
            else:assert r['state']=='needs_review' and b['repair_retry']['exhausted_at']
    assert not await pipeline.tick(db,lane='seed_repair')


@pytest.mark.asyncio
async def test_repair_success_preserves_review_and_forces_fresh_measurement(tmp_path,monkeypatch):
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db=Database(tmp_path);SourceLibrary(db);pipeline.schema(db)
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('CREATE TABLE plugin_reviews(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
        c.execute('INSERT INTO plugin_reviews VALUES(?,?,?,?)',(owner,'1','2','{}'))
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'1','2','needs_fields',json.dumps({'repair_retry':{'attempts':3}}),0,0))
    async def repair(*args):return {'state':'ready','reason':'complete_dossier'}
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    assert await pipeline.tick(db,lane='seed_repair')
    with db.connect() as c:
        r=c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
        assert r['state']=='queued' and 'repair_retry' not in json.loads(r['body'])
        assert c.execute('SELECT count(*) FROM plugin_reviews').fetchone()[0]==1
        body=json.loads(r['body'])
        assert body['force_full_evaluation'] is True
        archived=c.execute('SELECT body FROM repair_review_history WHERE id=?',(body['repair_review_revision'],)).fetchone()
        assert db.open(archived[0])=={}


@pytest.mark.asyncio
async def test_repair_does_not_overfill_busy_main_lane(tmp_path,monkeypatch):
    from flowhub.pipeline_modules.repair import PriceRepairModule
    from flowhub.pipeline_modules.admission import schema
    db=Database(tmp_path);SourceLibrary(db);schema(db)
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('INSERT INTO pipeline_campaigns VALUES(?,?,?,?)',(owner,1,json.dumps({'max_inflight':1}),0))
        for sku,state in [('1','needs_fields'),('2','queued')]:
            c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,sku,'3',state,'{}',0,0))
    async def repair(*args):pytest.fail('main capacity must be reserved before repairing')
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    assert not await pipeline.tick(db,lane='seed_repair')


@pytest.mark.asyncio
async def test_weight_first_profit_is_retained_while_publication_fields_are_repaired(tmp_path,monkeypatch):
    db=Database(tmp_path);lib=SourceLibrary(db)
    with db.connect() as c:owner=c.execute('SELECT id FROM users').fetchone()[0]
    lib.put(owner,{'sku':'1','seller_id':'2','title':'x','collected_at':100},{'channel':'test'})
    pipeline.enqueue(db,owner,'1','2')
    async def evaluate(*args):return {'state':'matched','candidate':{'origin':{'weight_first_valuation':True}}}
    async def publish(*args):pytest.fail('incomplete publication dossier')
    monkeypatch.setattr(pipeline,'evaluate',evaluate);monkeypatch.setattr(pipeline,'advance',publish)
    await pipeline.tick(db)
    with db.connect() as c:r=c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
    b=json.loads(r['body'])
    assert r['state']=='needs_fields'
    assert b['evaluation_state']=='matched'
    assert 'dimensions_mm' in b['pending_publication_fields']


@pytest.mark.asyncio
async def test_new_approved_product_must_enter_dossier_gate(tmp_path,monkeypatch):
    db=Database(tmp_path);lib=SourceLibrary(db)
    (tmp_path/'acquisition-policy.json').write_text('{"enabled":true}')
    with db.connect() as c:owner=c.execute('SELECT id FROM users').fetchone()[0]
    lib.put(owner,{'sku':'1','seller_id':'2','title':'x','collected_at':100},{'channel':'test'})
    pipeline.enqueue(db,owner,'1','2')
    async def evaluate(*args):return {'state':'matched'}
    async def publish(*args):pytest.fail('publication before dossier gate')
    monkeypatch.setattr(pipeline,'evaluate',evaluate);monkeypatch.setattr(pipeline,'advance',publish)
    await pipeline.tick(db)
    with db.connect() as c:
        r=c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
    assert r['state']=='needs_fields'
    assert json.loads(r['body'])['official_dossier_pending']
