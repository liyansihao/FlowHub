"""Normalize Maozi plugin's SKU-bound seller responses without inventing ranking facts."""
import copy
import math


def positive(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def enrich_plugin_detail(product, sales_packet, variant_packet):
    sku, seller = str(product['sku']), str(product['seller_id'])
    if sales_packet['sku'] != sku or variant_packet['sku'] != sku:
        raise ValueError('plugin_packet_identity_mismatch')
    result = variant_packet['result']
    if result.get('base', {}).get('status') != 200 or result.get('detail', {}).get('status') != 200:
        raise ValueError('plugin_detail_unavailable')
    matches = [v for v in result['base']['data'].get('variants', []) if sku in list(map(str, v.get('skus', [])))]
    if len(matches) != 1 or str(matches[0]['variant_id']) != str(result['variant']):
        raise ValueError('plugin_variant_identity_mismatch')
    base, detail = matches[0], result['detail']['data']['item']
    if str(detail.get('origin_variant_id')) != str(result['variant']):
        raise ValueError('plugin_template_identity_mismatch')
    p = copy.deepcopy(product)
    weight = positive(detail.get('weight'))
    dimensions = [positive(detail.get(k)) for k in ('depth', 'width', 'height')]
    p['plugin_detail'] = {
        'contract': 'maozi-plugin-sku-detail-v1', 'observed_at': variant_packet['observed_at'],
        'sku': sku, 'variant_id': str(result['variant']),
        'bundle_id': result['detail']['data'].get('bundle_id'),
        'category_path': base.get('categories'), 'description_type': base.get('description_type_dict_value'),
        'description_category_id': detail.get('description_category_id'),
        'description_type_name': base.get('description_type_name'),
        'brand': base.get('brand_name'),
        'is_copy_allowed': base.get('is_copy_allowed'),
        'is_content_copy_allowed': base.get('is_content_copy_allowed'),
        'weight_g': weight, 'dimensions_mm': dimensions,
        'attributes': detail.get('attributes'), 'sales_status': 'unavailable',
    }
    if weight is not None:
        p['weight_g'] = weight
    if all(dimensions):
        p['dimensions_cm'] = [v / 10 for v in dimensions]
    sales = sales_packet['result']
    if sales.get('status') == 200:
        rows = sales.get('data', {}).get('items', [])
        if any(str(r.get('sku')) != sku or str(r.get('sellerId')) != seller for r in rows) or len(rows) > 1:
            raise ValueError('plugin_sales_identity_mismatch')
        p['plugin_detail']['sales_status'] = 'present' if rows else 'empty_response'
        if rows:
            row = rows[0]
            p['plugin_detail']['monthly_sales'] = {
                'observed_at': sales_packet['observed_at'], 'provider_updated_at': sales['data'].get('updateDate'),
                'period': 'monthly', 'sold_count': row.get('soldCount'),
                'average_price_rub': row.get('avgPrice'), 'sales_sum_rub': row.get('soldSum'),
                'sales_schema': row.get('salesSchema'), 'blocked_by_seller': row.get('blockedBySeller'),
                'category_name': row.get('category3'), 'brand': row.get('brand'),
            }
            p['live_check'] = {'sku': sku, 'weight_g': weight, 'sales_schema': row.get('salesSchema'),
                               'observed_at': min(sales_packet['observed_at'], variant_packet['observed_at'])}
            p['verified_at'] = p['live_check']['observed_at']
    # Monthly sales, copy permission and category IDs retain their original semantics;
    # they are not silently converted into 28-day ranking sales or follow permission.
    return p
