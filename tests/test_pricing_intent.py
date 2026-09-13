import copy
from flowhub.pipeline_modules.pricing import approved_price_intent
from flowhub.plugin_publication import approved


def review():
    return {'state':'matched','finished_at':100,'candidate':{'source_key':'1','origin':{'price_evidence':{'value':30,'currency':'CNY','observed_at':-30000}}},
            'result':{'supplier_id':'2','purchase':3,'evidence':{'source':{'selected_offer_id':'2','selected_cost_cny':3,'comparebot':{'decision':{'outcome':'approved'}}},
            'profit':{'sell_price_cny':30,'input':{'sell_price':30,'purchase_price':3},'assessment':{'erp_profit_rate_pct':40}}}}}


def test_approved_asking_price_does_not_relabel_old_market_observation():
    r=review();prior=copy.deepcopy(r)
    intent=approved_price_intent(r,101)
    assert intent['value']==30 and intent['decided_at']==100
    assert intent['source_quote']['observed_at']==-30000
    approved(r,{},now=101,allow_unknown=True)
    assert r==prior


def test_no_intent_if_economics_or_approval_are_not_bound():
    for field,value in [('sell_price',31),('purchase_price',4)]:
        r=review();r['result']['evidence']['profit']['input'][field]=value
        assert approved_price_intent(r,101) is None
    r=review();r['state']='needs_review';assert approved_price_intent(r,101) is None
    assert approved_price_intent(review(),30000) is None
