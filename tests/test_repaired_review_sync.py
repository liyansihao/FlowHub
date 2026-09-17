import copy
import pytest
from test_plugin_comparebot import product
from flowhub.plugin_comparebot import candidate
from flowhub.pipeline_modules.dossier import repaired_review


def fixture():
    p=product();p['plugin_detail'].update(contract='maozi-plugin-sku-detail-v1',attributes=[{'id':1}])
    old=copy.deepcopy(p);old['plugin_detail']['attributes']=[]
    review={'state':'matched','finished_at':100,'candidate':candidate(old,now=101),
            'result':{'supplier_id':'supplier','purchase':4.1,'evidence':{
                'source':{'selected_offer_id':'supplier','selected_cost_cny':4.1,'comparebot':{'decision':{'outcome':'approved'}}},
                'profit':{'sell_price_cny':22.02,'input':{'sell_price':22.02,'purchase_price':4.1,
                    'package_weight':10,'package_length':15,'package_width':17,'package_height':1}}}}}
    return review,p


def test_complete_fields_sync_without_renewing_approval_or_repeating_procurement():
    review,p=fixture();prior=copy.deepcopy(review)
    synced=repaired_review(review,p,now=101)
    assert synced['candidate']['origin']['plugin_detail']['attributes']==[{'id':1}]
    assert synced['result']==review['result'] and synced['finished_at']==100
    assert 'publication_attributes_missing' not in synced['publication_blockers']
    assert review==prior


@pytest.mark.parametrize('change',['weight','price','identity','category','incomplete','expired'])
def test_changed_economics_identity_or_expiry_require_fresh_evaluation(change):
    review,p=fixture();now=101
    if change=='weight':p['plugin_detail']['weight_g']=20
    if change=='price':p['plugin_detail']['monthly_sales']['average_price_rub']=999
    if change=='identity':p['image']='https://example.com/other.jpg'
    if change=='category':p['plugin_detail']['description_type']='other'
    if change=='incomplete':p['plugin_detail']['attributes']=[]
    if change=='expired':now=22000
    assert repaired_review(review,p,now=now) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('manual',[False,True])
async def test_repair_queue_preserves_approval_state_with_same_profit(tmp_path,monkeypatch,manual):
    import json,time
    from flowhub.db import Database
    from flowhub.source_library import SourceLibrary
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db=Database(tmp_path);lib=SourceLibrary(db);pipeline.schema(db)
    review,p=fixture();now=time.time()
    if manual:
        review['state']='needs_review'
        review['result'].update(manual_review=True,reason='qwen_match_outside_safe_band')
        review['result']['evidence']['source']['comparebot']['decision']['outcome']='manual_review'
    review['finished_at']=now;p['collected_at']=now;p['plugin_detail']['observed_at']=now
    p['plugin_detail']['monthly_sales']['observed_at']=now
    review['candidate']['origin']['price_evidence']['observed_at']=now
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('CREATE TABLE plugin_reviews(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
        c.execute('INSERT INTO plugin_reviews VALUES(?,?,?,?)',(owner,'1','2',json.dumps(review)))
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'1','2','needs_fields','{}',0,0))
    lib.put(owner,p,{'channel':'test'})
    async def repair(*args):return {'state':'ready','reason':'complete_dossier'}
    async def evaluate(*args):pytest.fail('unchanged approved procurement must not be repeated')
    monkeypatch.setattr(PriceRepairModule,'run',repair);monkeypatch.setattr(pipeline,'evaluate',evaluate)
    await pipeline.tick(db,lane='seed_repair')
    with db.connect() as c:
        assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]==('needs_review' if manual else 'publishing')
        saved=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0])
        assert saved['result']==review['result'] and saved['finished_at']==now


def test_manual_comparison_receives_dossier_without_approval_or_clock_refresh():
    review,p=fixture();review['state']='needs_review'
    review['result'].update(manual_review=True,reason='qwen_match_outside_safe_band')
    review['result']['evidence']['source']['comparebot']['decision']['outcome']='manual_review'
    original=copy.deepcopy(review)
    assert repaired_review(review,p,now=101) is None
    synced=repaired_review(review,p,now=101,allow_manual=True)
    assert synced['state']=='needs_review' and synced['result']==original['result']
    assert synced['finished_at']==100
    assert 'publication_attributes_missing' not in synced['publication_blockers']
    assert review==original
    p['plugin_detail']['weight_g']=20
    assert repaired_review(review,p,now=101,allow_manual=True) is None
