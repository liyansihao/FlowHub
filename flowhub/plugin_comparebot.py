"""Source-library plugin facts -> the existing workspace compareBot/1688 matcher."""
import asyncio
import json
import time

from .comparebot import invoke
from .modules import ModuleError, image_url
from .plugin_detail import positive
from .source_delists import read_delists
from .source_library import SourceLibrary, fingerprint
from .evaluation_requirements import sale_price, publication_blockers


def candidate(product, now=None):
    now = time.time() if now is None else now
    detail = product.get('plugin_detail') or {}
    sales = detail.get('monthly_sales') or {}
    for observed in (detail.get('observed_at'),):
        if not isinstance(observed, (int, float)) or not 0 <= now - observed < 21600:
            raise ValueError('plugin_facts_missing_or_stale')
    if str(detail.get('sku')) != str(product['sku']):
        raise ValueError('plugin_identity_mismatch')
    quote=sale_price(product,now)
    weight, price = positive(detail.get('weight_g')), quote['value'] if quote else None
    dims = detail.get('dimensions_mm') or []
    if weight is None or price is None or len(dims) != 3 or any(positive(v) is None for v in dims):
        raise ValueError('pricing_facts_missing')
    relation = product.get('source_relation') or {}
    if relation.get('seller_id') != product['seller_id'] or not relation.get('root_seeds'):
        raise ValueError('source_provenance_missing')
    origin = dict(product)
    origin.update(cover_image=product['image'], product_url=product['url'],
                  category_name=sales.get('category_name') or detail.get('description_type_name') or product.get('category_name',''),
                  brand=sales.get('brand') or detail.get('brand') or (product.get('raw') or {}).get('brand',''),
                  average_price_rub=price if quote['currency']=='RUB' else None, price_evidence=quote,
                  profit_evaluation_only=True, publication_blockers=publication_blockers(product,now),
                  category_id=product.get('category_id') or detail.get('description_type'),
                  specifications={'attributes':detail.get('attributes'), 'variant_id':detail.get('variant_id')},
                  expansion_source={'contract':'flowhub-same-seller-v1','coverage':'storefront-exact-seller',
                                    'seed_bindings':relation['root_seeds'],'evidence_hash':product.get('source_relation_evidence_hash')})
    return {'source_key':str(product['sku']), 'title':product['title'], 'image':image_url(product['image']),
            'price':price, 'weight_g':weight, 'dimensions_cm':[float(v)/10 for v in dims],
            'pure_fbs':sales.get('sales_schema')=='FBS', 'price_currency':quote['currency'],
            **({'sell_price_cny':price} if quote['currency']=='CNY' else {}),
            'origin':origin, 'source_contract':'maozi-plugin-comparebot-v1'}


def eligible_roots(bindings, blocks, seller=None):
    blocked_offers={tuple(v) for v in blocks['offers']}
    result=[]
    for r in bindings:
        sku=str(r.get('source_sku') or r.get('sku') or '')
        if not sku.isdigit() or sku in blocks['skus']:continue
        shop,offer=r.get('shop'),r.get('offer')
        if shop is not None and offer is not None:
            if (str(shop),offer) in blocked_offers or ('*',offer) in blocked_offers:continue
        else:
            evidence=r.get('evidence') or {}
            if r.get('channel')!='other-seller-discovery' or evidence.get('channel')!='ozon-other-sellers-browser' or str(evidence.get('sku'))!=sku or not evidence.get('sha256'):
                continue
            if seller is not None and str(seller) not in evidence.get('sellers',[]):continue
        result.append(r)
    return result


async def evaluate(db, owner, sku, seller):
    library = SourceLibrary(db)
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_reviews(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))')
        row = c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        workflow = c.execute("SELECT * FROM workflows WHERE owner=? AND json_extract(modules,'$.matcher')='flowb-matcher'",(owner,)).fetchone()
        if row is None or workflow is None:
            raise ValueError('source_or_comparebot_workflow_missing')
        product = json.loads(row[0])
        if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone():
            raise ValueError('product_blocked')
    envelope = candidate(product)
    blocks = await read_delists()
    if sku in blocks['skus']:
        raise ValueError('explicit_delist')
    roots = eligible_roots(envelope['origin']['expansion_source']['seed_bindings'],blocks,seller)
    if not roots:
        raise ValueError('source_roots_delisted')
    envelope['origin']['expansion_source']['seed_bindings'] = roots
    started = time.time()
    matcher_secret=db.open(workflow['secrets'])['flowb-matcher']
    digest = fingerprint({'credential_revision':fingerprint(matcher_secret),'adapter_version':3,'plugin':product['plugin_detail'],'quote':envelope['origin']['price_evidence'],'roots':roots,'rules':workflow['rules']})
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        prior = c.execute('SELECT * FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        if prior and 0 <= started-prior['updated'] < (600 if prior['state']=='running' else 21600) and prior['state']!='error':
            saved=json.loads(prior['body'])
            retryable=(saved.get('result',{}).get('reason') or '').startswith('qwen_')
            if prior['state']=='running' or (saved.get('input_digest')==digest and not retryable):
                return saved | {'cached':True}
        c.execute('INSERT OR REPLACE INTO plugin_reviews VALUES(?,?,?,?,?,?)',(owner,sku,seller,'running',json.dumps({'state':'running','sku':sku,'input_digest':digest}),started))
    report={'sku':sku,'seller':seller,'started_at':started,'candidate':envelope,'submitted':False,'input_digest':digest,
            'publication_blockers':envelope['origin']['publication_blockers'],'price_basis':envelope['origin']['price_evidence']}
    try:
        secret=matcher_secret
        result=await asyncio.wait_for(invoke('match',{'candidate':envelope,'rules':json.loads(workflow['rules'])},secret),280)
        report['result']=result
        report['state']='needs_review' if result.get('manual_review') else 'rejected' if result.get('rejected') else 'matched'
        if report['state']=='matched':
            rate=result.get('evidence',{}).get('profit',{}).get('assessment',{}).get('erp_profit_rate_pct')
            if rate is None or float(rate)<=float(json.loads(workflow['rules']).get('profit_min',30)):
                report.update(state='rejected',reason='profit_below_workflow_threshold')
    except Exception as error:
        report.update(state='error',reason=str(error)[:180] if isinstance(error,ModuleError) else type(error).__name__)
    report['finished_at']=time.time()
    with db.connect() as c:
        c.execute('UPDATE plugin_reviews SET state=?,body=?,updated=? WHERE owner=? AND sku=? AND seller=?',
                  (report['state'],json.dumps(report),report['finished_at'],owner,sku,seller))
        # Read latest body so a concurrent collector cannot lose newer product facts.
        current=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        product=json.loads(current[0])
        product['listing_review']={'contract':'flowhub-listing-review-v1','module_state':report['state'],
            'observed_at':report['finished_at'],'label':'compareBot / 1688：'+report['state'],
            'reason':report.get('reason') or report.get('result',{}).get('reason'),
            'evidence_source':'plugin_reviews','publication_ready':False,
            'publication_blockers':report['publication_blockers'],'price_basis':report['price_basis']}
        library.put(owner,product,{'channel':'plugin-comparebot','observed_at':report['finished_at']},connection=c)
    return report
