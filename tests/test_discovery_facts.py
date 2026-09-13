import pytest
from flowhub.discovery_facts import merge


def test_direct_observations_never_become_28day_sales_or_follow_permission():
    p={'sku':'1','plugin_detail':{'description_category_id':20,'weight_g':15}}
    r=merge(p,{'data':{'sku':1,'soldCount':14,'soldSumRub':'1708','salesSchema':'FBS'}},100)
    assert r['direct_source_facts']['derived_unit_revenue_rub']==122
    assert r['direct_source_facts']['period']=='unspecified'
    assert r['category_id']=='20' and r['weight_g']==15
    assert 'sold_count_28d' not in r and 'average_price_rub' not in r and 'raw' not in r
    assert 'direct_source_facts' not in p
    with pytest.raises(ValueError,match='sku_mismatch'):merge(p,{'sku':2},100)
