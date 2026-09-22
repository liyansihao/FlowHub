"""Restore only audited approvals lost by the official dossier repair regression."""
import copy
import json

from ..identity_review import comparison,confirm,envelope
from ..plugin_publication import same_postal_package
from ..source_library import fingerprint


def restored_identity(receipt,product,current):
    if receipt['action']!='approve':raise ValueError('later_human_decision')
    snapshot=json.loads(receipt['body'])
    original=json.loads(snapshot['review'])
    before=original['candidate'];origin=before['origin'];physical=product.get('plugin_detail') or {}
    if (str(before['source_key'])!=str(product['sku']) or str(origin.get('seller_id'))!=str(product.get('seller_id'))
            or before.get('title')!=product.get('title') or before.get('image')!=product.get('image')):
        raise ValueError('source_identity_changed')
    old=origin.get('plugin_detail') or {}
    if old.get('variant_id') and old['variant_id']!=physical.get('variant_id'):
        raise ValueError('source_variant_changed')
    category=product.get('category_id') or physical.get('description_type')
    if origin.get('category_id') and str(origin['category_id'])!=str(category):
        raise ValueError('source_category_changed')
    dimensions=physical.get('dimensions_mm') or []
    if len(dimensions)!=3:raise ValueError('package_missing')
    package=dict(zip(('package_length','package_width','package_height'),dimensions))
    package['package_weight']=physical.get('weight_g')
    if not same_postal_package(package,original['result']['evidence']['profit']['input']):
        raise ValueError('approved_package_changed')
    # Added attributes can enrich the exact approved source. Changed known
    # attributes or later explicit model/brand conflicts cannot reuse approval.
    known={fingerprint(v) for v in physical.get('attributes') or []}
    if any(fingerprint(v) not in known for v in old.get('attributes') or []):
        raise ValueError('known_attributes_changed')
    latest=(current.get('identity_review') or {}).get('comparison') or comparison(current)
    if ((latest.get('decision') or {}).get('qwen_review') or {}).get('brand_or_model_conflict'):
        raise ValueError('explicit_identity_conflict')
    cb=copy.deepcopy((original.get('identity_review') or {}).get('comparison') or comparison(original))
    query=cb['search_and_rank']['query'];candidate=envelope(product)
    if str(query.get('product_id'))!=str(product['sku']) or query.get('title')!=product['title'] or query.get('image_url')!=product['image']:
        raise ValueError('receipt_comparison_unbound')
    previous=copy.deepcopy(query.get('specifications'))
    query['specifications']=candidate['origin']['specifications']
    original.setdefault('identity_review',{})['comparison']=cb
    result=confirm(original,receipt['actor'],receipt['note'])
    identity=result['identity_review']
    identity.pop('automatic_review',None)
    identity['human_review']['at']=receipt['at']
    identity.update(input_digest=fingerprint(candidate),reviewed_at=receipt['at'],
                    repair_restoration={'receipt_id':receipt['id'],'previous_specifications':previous,
                                        'reason':'exact_approved_source_with_supplementary_attributes'})
    result['state']='same_product_confirmed'
    return result
