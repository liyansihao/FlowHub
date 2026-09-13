import copy
import pytest
from flowhub.plugin_detail import enrich_plugin_detail


def packets():
    p={'sku':'1','seller_id':'2','sold_count_28d':7}
    s={'sku':'1','observed_at':10,'result':{'status':200,'data':{'items':[]}}}
    v={'sku':'1','observed_at':11,'result':{'variant':'3','base':{'status':200,'data':{'variants':[{'skus':['1'],'variant_id':'3','is_copy_allowed':True}]}},'detail':{'status':200,'data':{'bundle_id':'4','item':{'origin_variant_id':'3','weight':20,'depth':150,'width':180,'height':10}}}}}
    return p,s,v


def test_empty_sales_not_zero_and_units_preserved():
    p,s,v=packets(); got=enrich_plugin_detail(p,s,v)
    assert got['dimensions_cm']==[15,18,1]
    assert got['weight_g']==20 and got['sold_count_28d']==7
    assert got['plugin_detail']['sales_status']=='empty_response'
    assert 'live_check' not in got and 'raw' not in got


def test_monthly_sales_do_not_overwrite_28_day_count():
    p,s,v=packets();s['result']['data']['items']=[{'sku':'1','sellerId':'2','salesSchema':'FBS','soldCount':'26','avgPrice':202.456}]
    got=enrich_plugin_detail(p,s,v)
    assert got['sold_count_28d']==7 and got['live_check']['sales_schema']=='FBS'
    assert got['plugin_detail']['monthly_sales']['sold_count']=='26'


@pytest.mark.parametrize('kind',['seller','variant','template'])
def test_mismatched_identity_rejected(kind):
    p,s,v=packets()
    if kind=='seller': s['result']['data']['items']=[{'sku':'1','sellerId':'9'}]
    elif kind=='variant': v['result']['variant']='9'
    else: v['result']['detail']['data']['item']['origin_variant_id']='9'
    original=copy.deepcopy(p)
    with pytest.raises(ValueError): enrich_plugin_detail(p,s,v)
    assert p==original
