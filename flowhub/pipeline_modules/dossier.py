"""Reuse the exact reviewed source packet; never create another ERP draft for it."""
import time

STATIC_DOSSIER_MAX_AGE = 7 * 86400


def reviewed_snapshot(review, now=None):
    now=time.time() if now is None else now
    candidate=review['candidate'];origin=candidate['origin'];detail=origin.get('plugin_detail') or {}
    observed=detail.get('observed_at',0)
    website=bool(review.get('website_listing_authorization',{}).get('id') and review.get('identity_review',{}).get('verdict')=='match' and (review.get('identity_review',{}).get('human_review') or review.get('identity_review',{}).get('automatic_review')))
    if detail.get('contract') not in ('maozi-plugin-sku-detail-v1','maozi-erp-draft-detail-v1','direct-field-dossier-v1') or str(detail.get('sku'))!=str(candidate['source_key']):
        return None
    if not isinstance(observed,(int,float)) or observed<=0 or not 0<=now-observed or (not website and now-observed>=STATIC_DOSSIER_MAX_AGE):
        return None
    if detail.get('contract')=='direct-field-dossier-v1' and not website:
        for field in ('weight_g','dimensions_mm','attributes'):
            evidence=(detail.get('field_observations') or {}).get(field) or {}
            stamp=evidence.get('observed_at')
            if (str(evidence.get('sku'))!=str(candidate['source_key'])
                or evidence.get('source') not in ('maozi-erp-draft','maozi-category-by-sku','ozon-product-page')
                or not isinstance(stamp,(int,float)) or stamp<=0 or not 0<=now-stamp<STATIC_DOSSIER_MAX_AGE):
                return None
    dims=detail.get('dimensions_mm') or []
    if len(dims)!=3 or (not website and not detail.get('attributes')) or not candidate.get('title') or not candidate.get('image'):
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
                'attributes':detail.get('attributes') or [],'variant_id':detail.get('variant_id')}}


def require_unchanged_source(review, latest):
    """Older static facts are reusable only while newer known facts agree."""
    candidate=review['candidate'];origin=candidate['origin']
    if str(latest.get('sku'))!=str(candidate['source_key']) or str(latest.get('seller_id'))!=str(origin.get('seller_id')):
        raise ValueError('source_identity_changed')
    for field in ('title','image'):
        if latest.get(field) and latest[field]!=candidate.get(field):
            raise ValueError('source_comparison_changed')
    before=origin.get('plugin_detail') or {};after=latest.get('plugin_detail') or {}
    for field in ('weight_g','dimensions_mm','attributes','variant_id'):
        if after.get(field) and after[field]!=before.get(field):
            raise ValueError('source_package_or_attributes_changed')
    monthly=after.get('monthly_sales') or {}
    if monthly.get('blocked_by_seller') is True or monthly.get('sales_schema') not in (None,'','FBS'):
        raise ValueError('explicit_source_restriction')


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


def repaired_review(review, product, now=None, *, allow_manual=False):
    """Attach complete new facts to an unchanged, still-valid approved valuation."""
    import copy
    from ..plugin_comparebot import candidate
    from .pricing import approved_price_intent
    from ..plugin_publication import same_postal_package
    now=time.time() if now is None else now
    if approved_price_intent(review,now) is None:
        # Supplement a still-current human comparison without turning it into approval.
        # The automatic repair-to-publication path does not enable this option.
        if not allow_manual:return None
        try:
            import math
            result=review['result'];source=result['evidence']['source'];profit=result['evidence']['profit']
            decision=source['comparebot']['decision']
            price=float(profit['sell_price_cny']);cost=float(source['selected_cost_cny'])
            if (review['state']!='needs_review' or not result.get('manual_review')
                or not str(result.get('reason','')).startswith('qwen_')
                or decision.get('outcome')!='manual_review'
                or not source.get('selected_offer_id')
                or not 0<=now-review['finished_at']<21600
                or not math.isfinite(price) or price<=0 or not math.isfinite(cost) or cost<=0
                or price!=float(profit['input']['sell_price'])
                or cost!=float(profit['input']['purchase_price'])):return None
        except (KeyError,ValueError,TypeError):return None
    try:
        latest=candidate(product,now=now)
        old=review['candidate']
        if any(latest.get(k)!=old.get(k) for k in ('source_key','title','image')):return None
        if latest['origin'].get('seller_id')!=old['origin'].get('seller_id'):return None
        if latest['origin'].get('category_id')!=old['origin'].get('category_id'):return None
        quote=latest['origin']['price_evidence'];prior=old['origin']['price_evidence']
        if (quote['currency'],float(quote['value']))!=(prior['currency'],float(prior['value'])):return None
        updated=copy.deepcopy(review);updated['candidate']=latest
        snapshot=reviewed_snapshot(updated,now)
        if not snapshot or not same_postal_package(snapshot['detail'],review['result']['evidence']['profit']['input']):return None
        updated['publication_blockers']=latest['origin']['publication_blockers']
        updated['price_basis']=quote
        updated.setdefault('dossier_revisions',[]).append({'at':now,'previous_observed_at':old['origin'].get('plugin_detail',{}).get('observed_at'),'reason':'repaired_fields_same_approved_economics'})
        # Keep original approval time; supplementing attributes cannot renew Qwen approval.
        return updated
    except (ValueError,KeyError,TypeError):return None


def synchronize_repaired_review(connection, key, *, allow_manual=False):
    """Caller owns the queue lease and transaction; never overwrite a newer review."""
    import json
    row=connection.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    product=connection.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    if not row or not product:return False
    updated=repaired_review(json.loads(row[0]),json.loads(product[0]),allow_manual=allow_manual)
    if updated is None:return False
    connection.execute('UPDATE plugin_reviews SET body=? WHERE owner=? AND sku=? AND seller=?',(json.dumps(updated),*key))
    return True
