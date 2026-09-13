"""Reuse the exact reviewed source packet; never create another ERP draft for it."""
import time


def reviewed_snapshot(review, now=None):
    now=time.time() if now is None else now
    candidate=review['candidate'];origin=candidate['origin'];detail=origin.get('plugin_detail') or {}
    observed=detail.get('observed_at',0)
    if detail.get('contract') not in ('maozi-plugin-sku-detail-v1','maozi-erp-draft-detail-v1') or str(detail.get('sku'))!=str(candidate['source_key']):
        return None
    if not isinstance(observed,(int,float)) or not 0<=now-observed<21600:
        return None
    dims=detail.get('dimensions_mm') or []
    if len(dims)!=3 or not detail.get('attributes') or not candidate.get('title') or not candidate.get('image'):
        return None
    try:
        values=[float(v) for v in [*dims,detail.get('weight_g')]]
        if not all(0<v<float('inf') for v in values):return None
    except (TypeError,ValueError):
        return None
    return {'source_key':str(candidate['source_key']),'observed_at':observed,
            'source':'reviewed-plugin-packet','detail':{
                'title':candidate['title'],'package_length':values[0],
                'package_width':values[1],'package_height':values[2],'package_weight':values[3],
                'attributes':detail['attributes'],'variant_id':detail.get('variant_id')}}


def refresh_unchanged_plan(record, latest):
    """Refresh evidence before dispatch only if the immutable economics still match."""
    snapshot=reviewed_snapshot(latest)
    if not snapshot:raise ValueError('fresh_source_packet_required')
    plan=record['plan'];match=latest['result'];profit=match['evidence']['profit']
    if (str(match['supplier_id'])!=str(plan['supplier_identity'])
        or float(match['purchase'])!=float(plan['purchase_price_cny'])
        or float(profit['sell_price_cny'])!=float(plan['sell_price_cny'])):
        raise ValueError('prepared_plan_repricing_required')
    old=record['snapshot']['detail'];new=snapshot['detail']
    keys=('package_length','package_width','package_height')
    if sorted(float(old[k]) for k in keys)!=sorted(float(new[k]) for k in keys) or float(old['package_weight'])!=float(new['package_weight']):
        raise ValueError('prepared_plan_package_changed')
    record.setdefault('review_revisions',[]).append({'at':time.time(),'previous_review':record['review'],'reason':'fresh_price_same_economics'})
    record['review']=latest;record['snapshot']=snapshot


def refresh_procurement_plan(connection, journal_path, record, latest):
    """Version procurement only, atomically with FlowHub evidence; external intent stays fixed.

    Caller must hold the existing SKU/store locks and validate approved(latest).
    """
    import json
    from copy import deepcopy
    from ..source_library import fingerprint
    revised=deepcopy(record)
    revised['plan']['supplier_identity']=str(latest['result']['supplier_id'])
    revised['plan']['purchase_price_cny']=str(latest['result']['purchase'])
    refresh_unchanged_plan(revised,latest)  # requires same asking price and package
    if 'publication_journal' not in [r[1] for r in connection.execute('PRAGMA database_list')]:
        connection.execute('ATTACH DATABASE ? AS publication_journal',(str(journal_path),))
    row=connection.execute('SELECT plan,phase FROM publication_journal.zero_stock_tests WHERE offer_id=?',(record['offer_id'],)).fetchone()
    allowed=('prepared','ready','favorite_pending','reconciling','sync_pending','stock_ready','stock_pending','manual_review')
    if not row or row['phase'] not in allowed or json.loads(row['plan'])!=record['plan']:
        raise ValueError('plan_revision_conflict')
    if row['phase']=='manual_review':
        details=json.loads(connection.execute('SELECT details FROM publication_journal.zero_stock_tests WHERE offer_id=?',(record['offer_id'],)).fetchone()[0])
        if details.get('reason')!='reconciliation_timeout':raise ValueError('plan_revision_conflict')
    revision={'at':time.time(),'reason':'fresh_approved_procurement','previous_plan':record['plan'],
              'plan':revised['plan'],'review_digest':fingerprint(latest)}
    revised.setdefault('plan_revisions',[]).append(revision)
    changed=connection.execute('UPDATE publication_journal.zero_stock_tests SET plan=? WHERE offer_id=? AND phase=? AND plan=?',
        (json.dumps(revised['plan'],sort_keys=True),record['offer_id'],row['phase'],row['plan'])).rowcount
    if changed!=1:raise ValueError('plan_revision_conflict')
    return revised
