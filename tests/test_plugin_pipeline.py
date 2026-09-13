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
