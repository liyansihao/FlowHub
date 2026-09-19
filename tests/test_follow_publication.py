import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from flowhub import follow_publication as follow
from flowhub import plugin_publication as publication
from flowhub import plugin_pipeline as pipeline
from tests.test_official_migration import flow
from tests.test_repair_workflow import configured


def enable(db):
    (db.directory/'publication-policy.json').write_text(json.dumps({'backend':follow.BACKEND}))


@pytest.fixture
def native(flow, monkeypatch):
    db, owner, platform, review = flow
    enable(db)
    monkeypatch.setattr(follow,"feedback",AsyncMock(return_value={}))
    # No attributes/dossier at all: still valid for the ERP follow payload.
    review['candidate']['origin'].pop('ozon_dossier')
    review['publication_blockers']=['publication_attributes_missing']
    with db.connect() as c:
        c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(review),))
    from flowef.adapters.erp.maozi_test_listing import MaoziZeroStockAdapter
    monkeypatch.setattr(MaoziZeroStockAdapter,'target',AsyncMock(return_value=SimpleNamespace(
        shop=SimpleNamespace(active=True,warehouses=[SimpleNamespace(warehouse_id='88',active=True,name='嘉兴邮政测试')]),
        currency='CNY',watermark_id='9')))
    monkeypatch.setattr(MaoziZeroStockAdapter,'source_modes',AsyncMock(return_value=('FBS',)))
    from flowhub.pipeline_modules import request_bridge
    monkeypatch.setattr(request_bridge.PublicationBridge,'call',AsyncMock(return_value={}))
    calls=[]
    def erp(request):
        path=request.url.path
        body=json.loads(request.content) if request.content else {}
        calls.append((path,body))
        if path=='/api.product.favorite/lists':
            rows=[] if request.url.params.get('is_imported')=='1' else [{'id':101,'sku':'123','is_imported':0}]
            data={'data':rows,'total':len(rows)}
        elif path=='/api.product.import_logs/index':data={'data':[],'last_page':1}
        elif path=='/api.selection.follow/import':
            platform.offer=body['rows'][0]['offer_id']
            if platform.lost_import:raise httpx.ReadTimeout('ERP acknowledgement lost')
            data={'accepted':True}
        else:raise AssertionError('unexpected ERP endpoint '+path)
        return httpx.Response(200,json={'code':1,'data':data})
    monkeypatch.setattr(request_bridge,'MeasuredTransport',lambda bridge:httpx.MockTransport(erp))
    return db,owner,platform,review,calls


async def advance(native):
    db,owner,*_=native
    return await publication.advance(db,owner,'123','456')


async def finish(native):
    for _ in range(10):
        result=await advance(native)
        if result.get('verified'):return result
    pytest.fail('follow intent did not reach stock verification')


async def test_full_follow_path_without_dossier_or_collection(native):
    result=await finish(native)
    db,owner,platform,_,calls=native
    assert result['phase']=='stock_verified'
    payloads=[b for p,b in calls if p=='/api.selection.follow/import']
    assert len(payloads)==1
    payload=payloads[0]
    assert payload['shop_ids']==[11] and payload['watermark_id']==9
    assert set(payload['rows'][0])=={'id','sku','title','cover_image','link','sell_price','price','old_price','offer_id','brand','source','source_currency'}
    assert payload['rows'][0]['source']=='favorite'
    assert not any('/collect/' in p or 'description-category' in p for p,_ in calls+platform.calls)
    assert not any(p=='/v3/product/import' for p,_ in platform.calls)
    with db.connect() as c:
        record=json.loads(c.execute('SELECT body FROM plugin_publications').fetchone()[0])
        assert record['backend']==follow.BACKEND
        assert record['snapshot']['source']=='approved-valuation-inputs'
        assert c.execute("SELECT count(*) FROM jobs WHERE phase='selling'").fetchone()[0]==1


async def test_lost_import_response_and_policy_rollback_never_republish(native):
    db,owner,platform,*_=native
    assert (await advance(native))['phase']=='ready'
    platform.lost_import=True
    with pytest.raises(Exception):await advance(native)
    (db.directory/'publication-policy.json').write_text('{"backend":"existing"}')
    assert follow.selected(db,(owner,'123','456'))
    platform.lost_import=False
    assert (await finish(native))['verified']
    assert sum(p=='/api.selection.follow/import' for p,b in native[-1])==1


async def test_historical_official_record_keeps_official_readback(flow):
    db,owner,platform,_=flow
    assert (await publication.advance(db,owner,'123','456'))['phase']=='ready'
    enable(db)
    assert not follow.selected(db,(owner,'123','456'))
    assert (await publication.advance(db,owner,'123','456'))['phase']=='reconciling'
    assert (await publication.advance(db,owner,'123','456'))['verified']
    assert sum(p=='/v3/product/import' for p,b in platform.calls)==1


async def test_publication_repair_is_released_without_running_old_stages(tmp_path,monkeypatch):
    db,owner=configured(tmp_path);enable(db)
    from flowhub.pipeline_modules.repair import PriceRepairModule
    monkeypatch.setattr(PriceRepairModule,'run',AsyncMock(side_effect=AssertionError('old repair reached')))
    assert await pipeline.tick(db,lane='seed_repair',repair_kind='publication')
    with db.connect() as c:
        r=c.execute('SELECT state,body FROM plugin_pipeline').fetchone();b=json.loads(r['body'])
    assert r['state']=='queued' and b['publication_recheck']
    assert 'official_dossier_pending' not in b and 'repair_retry' not in b
    assert b['publication_migrations'][0]['previous']['repair_retry']['total_attempts']==10


async def test_approval_skips_both_new_and_old_dossier_gates(native,monkeypatch):
    db,owner,_,review,_=native
    (db.directory/'acquisition-policy.json').write_text('{"enabled":true}')
    review['candidate']['origin']['weight_first_valuation']=True
    monkeypatch.setattr(pipeline,'evaluate',AsyncMock(return_value=review))
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET state='queued'")
    assert await pipeline.tick(db,lane='review')
    with db.connect() as c:assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='publishing'


@pytest.mark.parametrize('change',['profit','review','restriction','expired'])
def test_follow_preserves_business_approval(native,change):
    review=copy.deepcopy(native[3])
    if change=='profit':review['result']['evidence']['profit']['assessment']['erp_profit_rate_pct']=20
    elif change=='review':review['state']='needs_review'
    elif change=='restriction':review['publication_blockers'].append('fresh_pure_fbs_required')
    else:review['finished_at']=time.time()-30000
    with pytest.raises(ValueError):publication.approved(review,{'profit_min':30},native_follow=True)


async def test_historical_unknown_source_never_recreates_or_imports(native):
    db,owner,*_=native
    from flowhub.source_detail import SourceCollector
    context={'owner':owner,'candidate':{'source_key':'123'},'store':{'credentials':{'erp_token':'must-not-be-used'}}}
    collector=SourceCollector(db,context)
    with db.connect() as c:c.execute('INSERT INTO source_details VALUES(?,?,?,?)',(collector.key,'favorite_started',db.seal({'favorite_attempted':True}),time.time()))
    with pytest.raises(ValueError,match='historical_source_write_unconfirmed'):await advance(native)
    assert not any(p in ('/api.product.favorite/toggle','/api.selection.follow/import') for p,_ in native[-1])


def test_valuation_and_existing_intents_are_not_migrated(native):
    db,owner,*_=native;body={'repair_retry':{'attempts':5}}
    assert not follow.release_repair(db,(owner,'123','456'),body)
    assert body=={'repair_retry':{'attempts':5}}


async def test_explicit_delist_still_prevents_follow_import(native,monkeypatch):
    monkeypatch.setattr(publication,'read_delists',AsyncMock(return_value={'skus':['123'],'offers':[]}))
    with pytest.raises(ValueError,match='explicit_delist'):await advance(native)
    assert not any(p=='/api.selection.follow/import' for p,_ in native[-1])


def test_existing_legacy_intent_is_never_adopted_by_follow_policy(native):
    db,owner,*_=native
    with db.connect() as c:
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?,?)',(owner,'123','456',json.dumps({'offer_id':'original','phase':'submitting'}),time.time()))
    assert not follow.selected(db,(owner,'123','456'))
    body={'official_dossier_pending':True}
    assert not follow.release_repair(db,(owner,'123','456'),body)
    assert body=={'official_dossier_pending':True}
