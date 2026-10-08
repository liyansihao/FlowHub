import copy
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_continuous_admission import setup as admission_setup

from flowhub import ranking_sources as ranking
from flowhub.db import Database
from flowhub.pipeline_modules import source_loop
from flowhub.pipeline_modules.admission import admit_one
from flowhub.source_library import SourceLibrary


def setup(tmp_path, categories=None):
    db, owner = admission_setup(tmp_path)
    source_loop.schema(db)
    config = {'enabled': True, 'owner': owner}
    if categories is not None:
        config['categories'] = categories
    (tmp_path / 'ranking-source-policy.json').write_text(json.dumps(config))
    with db.connect() as c:
        c.execute('INSERT INTO sourcing_settings VALUES(?,1,?,?)',
                  (owner, json.dumps({'sales_min': 1, 'pure_fbs': True}), db.seal({'erp_token': 'synthetic'})))
    ranking.activate(db, owner, 100)
    return db, owner


def row(sku='10', sales=3, **kwargs):
    return {'sku': sku, 'seller_id': '3', 'sold_count': sales, 'name': 'test',
            'photo': 'https://example.com/p.jpg', 'avg_price': 100, 'weight': 20,
            'cate2_id': '99', 'sales_schema': 'FBS', **kwargs}


async def refresh(db, owner, rows, now=100, last=1):
    request = AsyncMock(return_value={'data': rows, 'last_page': last})
    result = await ranking.refresh_one(db, owner, now, request, AsyncMock(return_value={'skus': [], 'offers': []}))
    return result, request


@pytest.mark.asyncio
async def test_ranking_first_expansion_fallback_and_new_ranking_returns_after_restart(tmp_path):
    db, owner = setup(tmp_path)
    await refresh(db, owner, [row()], 100)
    assert admit_one(db, owner, 101)['sku'] == '10'
    assert admit_one(db, owner, 102)['sku'] == '1'
    with db.connect() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM plugin_pipeline ORDER BY sku')]
    # A refreshed head finds a new SKU. Already admitted SKU10 is not replayed.
    result, request = await refresh(Database(tmp_path), owner, [row(), row('11')], 21700)
    assert result['added'] == 1
    assert request.call_args.args[1]['page'] == 1
    assert admit_one(db, owner, 21701)['sku'] == '11'
    with db.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM plugin_pipeline WHERE sku IN ('1','10') ORDER BY sku")] == before
        assert c.execute('SELECT COUNT(*) FROM pipeline_admissions').fetchone()[0] == 3


@pytest.mark.asyncio
async def test_pages_resume_do_not_treat_short_page_as_exhaustion_and_refresh_head(tmp_path):
    db, owner = setup(tmp_path)
    result, _ = await refresh(db, owner, [row()], 100, None)
    result, request = await refresh(Database(tmp_path), owner, [row('11')], 131, None)
    assert request.call_args.args[1]['page'] == 2
    result, request = await refresh(db, owner, [row('12')], 21700, None)
    assert request.call_args.args[1]['page'] == 1
    result, request = await refresh(db, owner, [row('13')], 21731, None)
    assert request.call_args.args[1]['page'] == 3


@pytest.mark.asyncio
async def test_empty_ranking_refreshes_later_and_expansion_continues(tmp_path):
    db, owner = setup(tmp_path)
    result, _ = await refresh(db, owner, [], 100)
    assert result['state'] == 'ranking_refreshed'
    assert admit_one(db, owner, 101)['sku'] == '1'
    result, request = await refresh(db, owner, [row()], 102)
    assert result['state'] == 'no_due_ranking'
    request.assert_not_called()
    assert (await refresh(db, owner, [row()], 21700))[0]['added'] == 1
    assert admit_one(db, owner, 21701)['sku'] == '10'


@pytest.mark.asyncio
async def test_network_failure_and_repeated_page_keep_cursor_and_do_not_block_expansion(tmp_path):
    db, owner = setup(tmp_path)
    await refresh(db, owner, [row()], 100, None)
    result, _ = await refresh(db, owner, [row()], 131, None)
    assert result['state'] == 'ranking_retry'
    fail = AsyncMock(side_effect=TimeoutError)
    result = await ranking.refresh_one(db, owner, 300, fail, AsyncMock(return_value={'skus': [], 'offers': []}))
    assert result['state'] == 'ranking_retry'
    assert fail.call_args.args[1]['page'] == 2
    with db.connect() as c:
        assert c.execute('SELECT page FROM source_ranking_pages').fetchone()[0] == 2
    assert admit_one(db, owner, 301)['sku'] == '10'
    assert admit_one(db, owner, 302)['sku'] == '1'


@pytest.mark.asyncio
async def test_categories_take_turns_and_provider_mismatch_cannot_advance(tmp_path):
    db, owner = setup(tmp_path, ['98', '99'])
    result, request = await refresh(db, owner, [row(cate2_id='98')], 100, None)
    assert request.call_args.args[1]['category2'] == '98'
    result, request = await refresh(db, owner, [row('11')], 101, None)
    assert request.call_args.args[1]['category2'] == '99'
    result, request = await refresh(db, owner, [row('12')], 131, None)
    assert result['state'] == 'ranking_retry'
    with db.connect() as c:
        assert c.execute("SELECT page FROM source_ranking_pages WHERE category='98'").fetchone()[0] == 2
        assert not c.execute("SELECT 1 FROM sourcing_products WHERE sku='12'").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [0, None, -1, True, 'unknown'])
async def test_only_positive_sales_enter_ranking_pool(tmp_path, value):
    db, owner = setup(tmp_path)
    result, _ = await refresh(db, owner, [row(sales=value)])
    assert result['added'] == 0
    assert admit_one(db, owner, 101)['sku'] == '1'


@pytest.mark.asyncio
async def test_old_provider_report_not_made_fresh_and_mean_price_not_asking_price(tmp_path):
    db, owner = setup(tmp_path)
    now = time.time()
    result, _ = await refresh(db, owner, [row(update_time=now-8*86400), row('11')], now)
    assert result['added'] == 1
    assert admit_one(db, owner, now+1)['sku'] == '11'
    with db.connect() as c:
        p = json.loads(c.execute("SELECT body FROM sourcing_products WHERE sku='11'").fetchone()[0])
        assert p['average_price_rub'] == 100
        assert p['current_price_rub'] is None
        assert 'proposed_sale_price' not in p
        assert c.execute("SELECT state FROM plugin_pipeline WHERE sku='11'").fetchone()[0] == 'needs_fields'


@pytest.mark.asyncio
async def test_blocked_and_handled_goods_are_never_readmitted_and_facts_not_overwritten(tmp_path):
    db, owner = setup(tmp_path)
    with db.connect() as c:
        c.execute('INSERT INTO blocks(owner,source_key,reason) VALUES(?,?,?)', (owner,'10','manual'))
        old = c.execute("SELECT body FROM sourcing_products WHERE sku='2'").fetchone()[0]
    await refresh(db, owner, [row(), row('2')])
    with db.connect() as c:
        assert c.execute("SELECT body FROM sourcing_products WHERE sku='2'").fetchone()[0] == old
    assert admit_one(db, owner, 101)['sku'] == '2'
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='rejected'")
    await refresh(db, owner, [row(), row('2')], 21700)
    assert admit_one(db, owner, 21701)['sku'] == '1'


@pytest.mark.asyncio
async def test_rank_update_zero_removes_priority_without_resetting_old_candidates(tmp_path):
    db, owner = setup(tmp_path)
    await refresh(db, owner, [row('2')])
    await refresh(db, owner, [row('2', 0)], 21700)
    assert admit_one(db, owner, 21701)['sku'] == '1'


@pytest.mark.asyncio
async def test_existing_seed_preserved_new_publication_requires_own_sales(tmp_path):
    db, owner = admission_setup(tmp_path)
    source_loop.schema(db)
    with db.connect() as c:
        c.execute('INSERT INTO sourcing_seeds VALUES(?,?,?,?,?,0,NULL,?)',
                  (owner,'1','old','90',json.dumps({'seller_id':'9'}),100))
    (tmp_path/'ranking-source-policy.json').write_text(json.dumps({'enabled':True,'owner':owner}))
    ranking.activate(db, owner, 100)
    with db.connect() as c:
        c.execute('INSERT INTO sourcing_settings VALUES(?,1,?,?)',(owner,'{}',db.seal({'erp_token':'synthetic'})))
        c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL)')
        p={'verified':True,'sku':'10','seller':'3','offer_id':'new','product':{'shop_id':'1'}}
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?,?)',(owner,'10','3',json.dumps(p),100))
    source_loop.sync_stores(db, owner, 'scan', 101)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM sourcing_seeds').fetchone()[0] == 1
        assert c.execute("SELECT 1 FROM source_loop_stores WHERE seller='9'").fetchone()
    # Same SKU, wrong seller cannot qualify this root.
    request=AsyncMock(return_value={'data':[row(seller_id='4')], 'last_page':1})
    result=await ranking.verify_seed_one(db, owner, 102, request)
    assert not result['qualified']
    source_loop.sync_stores(db, owner, 'scan', 103)
    request=AsyncMock(return_value={'data':[row()], 'last_page':1})
    assert (await ranking.verify_seed_one(db, owner, 21702, request))['qualified']
    source_loop.sync_stores(db, owner, 'scan', 21703)
    with db.connect() as c:
        seed=c.execute("SELECT * FROM sourcing_seeds WHERE offer='new'").fetchone()
        assert seed['sku']=='10'
        assert json.loads(seed['body'])['sales_qualification']['evidence']['sold_count']==3
        assert c.execute('SELECT COUNT(*) FROM source_legacy_seeds').fetchone()[0] == 1
    source_loop.sync_stores(Database(tmp_path), owner, 'scan', 21704)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM sourcing_seeds').fetchone()[0]==2


def evidence(now=100):
    return {'contract':ranking.CONTRACT, 'endpoint':'/api.selection.top/lists', 'sku':'1',
            'seller_id':'2', 'sold_count':2, 'observed_at':now, 'period':'28d', 'evidence_hash':'test'}


def test_new_contract_has_no_fake_root_and_retains_fact_checks():
    from test_plugin_comparebot import product

    from flowhub.plugin_comparebot import candidate
    p=product();p.pop('source_relation');p.update(coverage='sales-ranking',ranking_source=evidence())
    c=candidate(p,101)
    assert c['origin']['expansion_source']['contract']==ranking.CONTRACT
    assert 'seed_bindings' not in c['origin']['expansion_source']
    for changed in [dict(evidence(),sku='99'),dict(evidence(),sold_count=0),dict(evidence(),observed_at=-700000)]:
        bad=copy.deepcopy(p);bad['ranking_source']=changed
        with pytest.raises(ValueError,match='source_provenance_missing'):candidate(bad,101)
    p['plugin_detail']['sku']='99'
    with pytest.raises(ValueError,match='plugin_identity_mismatch'):candidate(p,101)


def test_javascript_ranking_feedback_keeps_hard_exclusions_and_identity(tmp_path):
    bridge=(Path(__file__).resolve().parents[1]/'bridges/follow-feedback.mjs').as_uri()
    p={'sku':'1','seller_id':'2','weight_first_valuation':True,'profit_evaluation_only':True,
       'valuation_weight_g':100,'plugin_detail':{'sku':'1'},
       'expansion_source':{'contract':ranking.CONTRACT,'coverage':'sales-ranking','ranking':evidence()},
       'price_evidence':{'value':50,'currency':'CNY','observed_at':100}}
    script="""
import assert from 'node:assert/strict';
import {checkFollowFeedback} from BRIDGE;
const p=PRODUCT, s={comparebot:{decision:{outcome:'approved'}}};
const deps={engine:{categoryPolicyFor:()=>({eligible:false}),prohibitedCategoryMatch:()=>false},
 rules:{},cfg:{flow_f:{blocked_source_brands:[]}},feedback:{},
 feedbackBlockForProduct:()=>({blocked:false}),feedbackBlockForPair:()=>({blocked:false}),
 blockedImportBrand:()=>false,verifiedPluginSource:()=>false,verifiedPluginEvaluation:()=>false};
assert.deepEqual(checkFollowFeedback(p,s,deps,101),{blocked:false});
assert.deepEqual(checkFollowFeedback(p,s,{...deps,blockedImportBrand:()=>true},101),{rejected:'protected_product'});
assert.deepEqual(checkFollowFeedback(p,s,{...deps,feedbackBlockForPair:()=>({blocked:true})},101),{blocked:true});
p.expansion_source.ranking.seller_id='99';
assert.deepEqual(checkFollowFeedback(p,s,deps,101),{rejected:'category_not_prioritized'});
""".replace('BRIDGE',json.dumps(bridge)).replace('PRODUCT',json.dumps(p))
    result=subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', [None, 'wrong_seller', 'lease', 'lost_ack'])
async def test_ranking_favorite_archive_keeps_evidence_and_existing_guards(tmp_path, guard):
    from test_favorite_cleanup import Fake
    from test_favorite_cleanup import setup as cleanup_setup

    from flowhub.pipeline_modules import favorite_cleanup as cleanup
    db, owner, item=cleanup_setup(tmp_path)
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p.pop('source_relation')
        p.update(coverage='sales-ranking',ranking_source=dict(evidence(),sku='123'))
        if guard=='wrong_seller':p['ranking_source']['seller_id']='99'
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
        if guard=='lease':
            c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'123','2','live',time.time()+60))
    api=Fake('lost_ack' if guard=='lost_ack' else 'ok',db)
    await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
    assert api.writes==(0 if guard in ('wrong_seller','lease') else 1)
    if api.writes:
        with db.connect() as c:
            saved=db.open(c.execute('SELECT body FROM favorite_cleanup_receipts').fetchone()[0])
        assert saved['root_seeds']==[]
        assert json.loads(saved['product']['body'])['ranking_source']['sku']=='123'
        assert Path(saved['archive_path']).exists()


@pytest.mark.asyncio
async def test_ranking_refresh_still_runs_when_old_expansion_backlog_is_full(tmp_path,monkeypatch):
    db,owner=setup(tmp_path)
    request=AsyncMock(return_value={'data':[row()], 'last_page':1})
    monkeypatch.setattr('flowhub.source_acquisition.erp_request',request)
    monkeypatch.setattr('flowhub.source_delists.read_delists',AsyncMock(return_value={'skus':[],'offers':[]}))
    monkeypatch.setattr(source_loop,'resolve_one',AsyncMock(return_value={'state':'no_due_seed'}))
    result=await source_loop.tick(db,{'enabled':True,'owner':owner,'run_id':'scan','max_source_backlog':1})
    assert result['state']=='source_backpressure'
    request.assert_awaited_once()
    with db.connect() as c:assert c.execute("SELECT 1 FROM sourcing_products WHERE sku='10'").fetchone()


@pytest.mark.asyncio
async def test_pause_during_refresh_keeps_cursor_and_creates_no_candidate(tmp_path):
    from flowhub.pipeline_modules.control import set_paused
    db,owner=setup(tmp_path)
    async def request(*args):
        set_paused(db,'seed',True)
        return {'data':[row()], 'last_page':1}
    result=await ranking.refresh_one(db,owner,100,request,AsyncMock(return_value={'skus':[],'offers':[]}))
    assert result['state']=='paused'
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM sourcing_products WHERE sku='10'").fetchone()
        assert c.execute('SELECT page,refresh_due FROM source_ranking_pages').fetchone()[:]==(1,0)


@pytest.mark.asyncio
async def test_latest_zero_sales_does_not_reuse_old_monthly_positive(tmp_path):
    db,owner=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE sourcing_products SET body=json_set(body,'$.plugin_detail',json(?)) WHERE sku='1'",
                  (json.dumps({'sku':'1','monthly_sales':{'period':'monthly','sold_count':99,'observed_at':90}}),))
    await refresh(db,owner,[row('1',0)],100)
    with db.connect() as c:assert ranking.demand_evidence(c,owner,'1','3',101) is None


def test_exploration_cannot_bypass_new_seed_sales_and_old_discovery_is_kept(tmp_path):
    from flowhub import other_sellers
    db,owner=setup(tmp_path);other_sellers.schema(db)
    p={'sku':'10','seller_id':'3','title':'t','image':'https://example.com/i',
       'collected_at':100,'coverage':'storefront-page',
       'source_relation':{'seller_id':'3','root_seeds':[{'sku':'9','channel':'other-seller-discovery'}]}}
    SourceLibrary(db).put(owner,p,{'channel':'test'})
    with db.connect() as c:
        c.execute('INSERT INTO source_discovery_seeds VALUES(?,?,?,0,0,?,?)',(owner,'90','queued','{}',90))
    assert other_sellers.replenish(db,owner,now=101,explore_pending=True)==0
    with db.connect() as c:
        assert c.execute('SELECT sku FROM source_discovery_seeds').fetchone()[0]=='90'
        assert c.execute('SELECT COUNT(*) FROM source_discovery_seeds').fetchone()[0]==1


@pytest.mark.asyncio
async def test_existing_ranking_proof_refresh_preserves_review_price_and_queue(tmp_path):
    db,owner=setup(tmp_path)
    await refresh(db,owner,[row()],100)
    admit_one(db,owner,101)
    with db.connect() as c:
        before=json.loads(c.execute("SELECT body FROM sourcing_products WHERE sku='10'").fetchone()[0])
        queue=tuple(c.execute("SELECT * FROM plugin_pipeline WHERE sku='10'").fetchone())
    result,_=await refresh(db,owner,[row(sales=4)],100+8*86400)
    assert result['added']==0
    with db.connect() as c:
        after=json.loads(c.execute("SELECT body FROM sourcing_products WHERE sku='10'").fetchone()[0])
        assert tuple(c.execute("SELECT * FROM plugin_pipeline WHERE sku='10'").fetchone())==queue
        proof=after.pop('ranking_source');before.pop('ranking_source')
        assert after==before
        assert proof['observed_at']==100+8*86400 and proof['sold_count']==4
        saved=c.execute('SELECT body FROM sourcing_evidence WHERE owner=? AND hash=?',(owner,proof['evidence_hash'])).fetchone()
        assert json.loads(saved[0])['row']['sold_count']==4


@pytest.mark.asyncio
async def test_ranking_follow_flag_is_advisory_without_changing_other_source_rules(tmp_path):
    from flowhub.source_library import SourceFilters, assess
    from flowhub.plugin_publication import require_source_modes
    db, owner = setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE sourcing_settings SET body=? WHERE owner=?",
                  (json.dumps({'sales_min': 1, 'pure_fbs': True, 'require_follow_allowed': True}), owner))
    result, _ = await refresh(db, owner, [row(blocked_by_seller=True)])
    assert result['added'] == 1
    assert admit_one(db, owner, 101)['sku'] == '10'
    with db.connect() as c:
        p = json.loads(c.execute("SELECT body FROM sourcing_products WHERE sku='10'").fetchone()[0])
        evidence = json.loads(c.execute('SELECT body FROM sourcing_evidence WHERE hash=?',
                                       (p['ranking_source']['evidence_hash'],)).fetchone()[0])
    assert p['raw']['blocked_by_seller'] is True
    assert evidence['row']['blocked_by_seller'] is True
    filters = SourceFilters(require_follow_allowed=True)
    assessment = assess(p, filters, 101)
    assert 'follow_allowed' not in assessment['failed']
    assert 'follow_allowed' in assessment['missing']
    assert 'follow_allowed' not in assessment['passed']
    # A label alone, mismatched evidence or a non-ranking source cannot relax the old rule.
    for change in ({'coverage': 'storefront-page'}, {'ranking_source': {}},
                   {'ranking_source': dict(p['ranking_source'], seller_id='99')}):
        assert 'follow_allowed' in assess(p | change, filters, 101)['failed']
    with pytest.raises(ValueError, match='seller_explicitly_blocks_follow'):
        require_source_modes(('FBS',), {'blocked_by_seller': True}, allow_unknown=True, now=101)
    # Refresh does not replay the admitted item or overwrite its decision.
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='rejected' WHERE sku='10'")
    assert (await refresh(db, owner, [row(blocked_by_seller=True)], 21700))[0]['added'] == 0
    assert admit_one(db, owner, 21701)['sku'] == '1'


@pytest.mark.asyncio
async def test_advisory_ranking_flag_keeps_sales_price_weight_fbs_and_explicit_blocks(tmp_path):
    db, owner = setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE sourcing_settings SET body=? WHERE owner=?",
                  (json.dumps({'sales_min': 1, 'price_min': 100, 'price_max': 1500,
                               'weight_max_g': 500, 'pure_fbs': True, 'require_follow_allowed': True}), owner))
        c.execute('INSERT INTO blocks(owner,source_key,reason) VALUES(?,?,?)', (owner, '17', 'manual'))
    rows = [row('10', blocked_by_seller=True), row('11', sales=0, blocked_by_seller=True),
            row('12', avg_price=99, blocked_by_seller=True), row('13', avg_price=1501, blocked_by_seller=True),
            row('14', weight=501, blocked_by_seller=True), row('15', sales_schema='FBO', blocked_by_seller=True),
            row('16', update_time=1, blocked_by_seller=True), row('17', blocked_by_seller=True),
            row('18', blocked_by_seller=True)]
    result = await ranking.refresh_one(db, owner, 700000, AsyncMock(return_value={'data': rows, 'last_page': 1}),
                                      AsyncMock(return_value={'skus': ['18'], 'offers': []}))
    assert result['added'] == 1
    assert admit_one(db, owner, 700001)['sku'] == '10'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sourcing_products WHERE sku IN ('11','12','13','14','15','16','17','18')").fetchone()[0] == 0
