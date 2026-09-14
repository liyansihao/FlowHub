import copy
import pytest
from flowhub.plugin_comparebot import candidate


def product():
    return {'sku':'1','seller_id':'2','title':'Paper','image':'https://example.com/image.jpg','url':'https://www.ozon.ru/product/1/',
            'sold_count_28d':9,'source_relation':{'seller_id':'2','root_seeds':[{'sku':'3','shop':'4','offer':'a'}]},
            'plugin_detail':{'sku':'1','observed_at':100,'description_type':'91401','weight_g':10,'dimensions_mm':[150,170,10],
                             'monthly_sales':{'observed_at':100,'sales_schema':'FBS','blocked_by_seller':False,'average_price_rub':220.2}}}


def test_plugin_to_existing_matcher_preserves_price_units_and_provenance():
    p=product();prior=copy.deepcopy(p);c=candidate(p,now=101)
    assert c['origin']['cover_image']==p['image']
    assert c['dimensions_cm']==[15,17,1]
    assert c['origin']['average_price_rub']==220.2
    assert c['origin']['sold_count_28d']==9
    assert c['origin']['price_evidence']['period']=='monthly'
    assert c['origin']['expansion_source']['seed_bindings']==p['source_relation']['root_seeds']
    assert p==prior


@pytest.mark.parametrize('kind',['weight','identity','price','roots'])
def test_incomplete_plugin_facts_do_not_reach_paid_matching(kind):
    p=product();d=p['plugin_detail']
    if kind=='weight':d['weight_g']=None
    if kind=='identity':d['sku']='9'
    if kind=='mode':d['monthly_sales']['sales_schema']='FBO'
    if kind=='follow':d['monthly_sales']['blocked_by_seller']=None
    if kind=='dimensions':d['dimensions_mm']=[0,170,10]
    if kind=='price':d['monthly_sales']['average_price_rub']=None
    if kind=='roots':p['source_relation']['root_seeds']=[]
    with pytest.raises(ValueError):candidate(p,now=101)


def test_unknown_shipping_and_follow_allow_evaluation_but_block_publication():
    p=product();p['plugin_detail']['monthly_sales'].update(sales_schema=None,blocked_by_seller=None)
    c=candidate(p,now=101)
    assert not c['pure_fbs']
    assert 'fresh_pure_fbs_required' in c['origin']['publication_blockers']


def test_no_monthly_sales_uses_cny_display_without_currency_conversion():
    p=product();p['plugin_detail'].pop('monthly_sales');p.update(current_price_display='49,71\\u2009¥',collected_at=100)
    c=candidate(p,now=101)
    assert c['sell_price_cny']==49.71 and c['price_currency']=='CNY'
    assert c['origin']['average_price_rub'] is None
    assert c['origin']['publication_blockers']


def test_discovery_roots_need_evidence_but_not_own_offer():
    from flowhub.plugin_comparebot import eligible_roots
    root={'sku':'123','channel':'other-seller-discovery','evidence':{'sku':'123','channel':'ozon-other-sellers-browser','sha256':'hash'}}
    assert eligible_roots([root],{'skus':[],'offers':[]})==[root]
    assert eligible_roots([root],{'skus':['123'],'offers':[]})==[]
    assert eligible_roots([{'sku':'123'}],{'skus':[],'offers':[]})==[]
    assert eligible_roots([{'sku':'123','shop':'1','offer':'x'}],{'skus':[],'offers':[['*','x']]})==[]


def test_discovery_root_must_include_the_actual_source_seller():
    from flowhub.plugin_comparebot import eligible_roots
    root={'sku':'123','channel':'other-seller-discovery','evidence':{'sku':'123','channel':'ozon-other-sellers-browser','sha256':'hash','sellers':['20']}}
    assert eligible_roots([root],{'skus':[],'offers':[]},'20')==[root]
    assert eligible_roots([root],{'skus':[],'offers':[]},'21')==[]


def test_static_dossier_age_and_missing_dimensions_do_not_block_valuation():
    p=product();p['plugin_detail'].update(observed_at=-30000,dimensions_mm=[])
    c=candidate(p,now=101)
    assert c['dimensions_cm'] is None
    assert c['origin']['weight_first_valuation'] is True
    assert c['origin']['valuation_weight_g']==10
    assert c['origin']['commission_fallback_pct']==12
    p['plugin_detail']['monthly_sales']['observed_at']=-30000
    with pytest.raises(ValueError):candidate(p,now=101)
