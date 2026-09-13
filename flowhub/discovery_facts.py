"""Exact-SKU ERP observations for discovery; preserve provider period semantics."""
import copy,re
from .source_library import numeric


def merge(product,packet,observed_at):
    data=packet.get('data',packet)
    if str(data.get('sku'))!=str(product['sku']):raise ValueError('direct_sku_mismatch')
    p=copy.deepcopy(product);detail=p.get('plugin_detail') or {};facts={
        'sku':str(data['sku']),'observed_at':observed_at,'provider':'maozi-sku3','period':'unspecified'}
    for name in ('soldCount','soldSumRub','salesSchema','category','category_ids','custom_weight','custom_volume'):
        if name in data:facts[name]=data[name]
    count=numeric(data.get('soldCount'));total=numeric(data.get('soldSumRub'))
    if count and total is not None:facts['derived_unit_revenue_rub']=total/count
    # This is NOT a 28-day count/average or proof of follow permission.
    p['direct_source_facts']=facts
    category=detail.get('description_category_id')
    if category:p.setdefault('category_id',str(category))
    if data.get('category'):p['category_name']=data['category']
    weight=detail.get('weight_g')
    raw_weight=str(data.get('custom_weight') or '')
    m=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*g\s*',raw_weight)
    if m:weight=float(m[1])
    if numeric(weight) is not None:p['weight_g']=weight
    if data.get('salesSchema') in ('FBS','FBO','RFBS'):
        p['sales_schema']=data['salesSchema']
        p['live_check']={'sku':p['sku'],'observed_at':observed_at,'sales_schema':data['salesSchema'],'weight_g':weight}
    return p
