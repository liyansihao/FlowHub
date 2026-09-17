"""Portable extraction of observed ERP draft data; never invents missing facts."""
def extract(payload):
    sku=str(payload['sku']);snapshot=payload['snapshot']
    if str(snapshot.get('source_key'))!=sku:raise ValueError('draft_source_mismatch')
    detail=snapshot.get('detail') or {}
    if len(detail.get('skus') or [])!=1:raise ValueError('ambiguous_draft_variant')
    return {'sku':sku,'fields':{
        'weight_g':detail.get('package_weight'),
        'dimensions_mm':[detail.get(k) for k in ('package_length','package_width','package_height')],
        'attributes':detail.get('common_attributes')}}
