"""Field-specific Maozi ERP supplementation, independent of publication deadlines."""
import json
import time
from ..maozi import MaoziPublisher
from ..source_library import SourceLibrary
from ..evaluation_requirements import sale_price
from ..plugin_detail import positive


def missing_fields(product):
    detail=product.get('plugin_detail') or {};missing=[]
    for key in ('title','image','url'):
        if not product.get(key):missing.append(key)
    if not positive(detail.get('weight_g')):missing.append('weight_g')
    dims=detail.get('dimensions_mm') or []
    if len(dims)!=3 or not all(positive(v) for v in dims):missing.append('dimensions_mm')
    if not detail.get('attributes'):missing.append('attributes')
    observed=detail.get('observed_at')
    if not isinstance(observed,(int,float)) or not 0<=time.time()-observed<21600:
        # Match publication's existing static-fact policy; keep original timestamps.
        # reviewed_snapshot also requires the exact SKU, complete fields and provenance.
        from .dossier import reviewed_snapshot
        snapshot=reviewed_snapshot({'candidate':{'source_key':str(product.get('sku','')),
            'title':product.get('title'),'image':product.get('image'),'origin':product}})
        if snapshot is None:missing.append('fresh_dossier')
    if sale_price(product) is None:missing.append('sale_price')
    return missing


def valuation_ready(product):
    from ..plugin_comparebot import candidate
    try:
        candidate(product)
        return True
    except (ValueError,KeyError):
        return False


def merge_draft(product, snapshot, *, now=None, packet=None):
    if str(snapshot.get('source_key'))!=str(product['sku']):raise ValueError('draft_source_mismatch')
    detail=snapshot.get('detail') or {}
    if len(detail.get('skus') or [])!=1:raise ValueError('ambiguous_draft_variant')
    p=json.loads(json.dumps(product))
    from ..dossier_packet import extract
    observed=extract({'sku':str(product['sku']),'snapshot':snapshot})
    if packet is not None and packet!=observed:raise ValueError('remote_dossier_evidence_mismatch')
    fields=(packet or observed)['fields']
    from .direct_facts import merge_fields
    p,applied=merge_fields(p,fields,'maozi-erp-draft',snapshot['observed_at'],now=now)
    merged=p['plugin_detail'];merged['erp_draft_id']=snapshot.get('draft_id')
    categories=detail.get('category_id') or []
    if len(categories)>1 and not merged.get('description_category_id'):merged['description_category_id']=categories[1]
    if not p.get('title') and detail.get('title'):p['title']=detail['title']
    if not p.get('url') and detail.get('url'):p['url']=detail['url']
    images=detail.get('images') or []
    if not p.get('image') and images and isinstance(images[0],str):p['image']=images[0]
    return p


async def direct_identity_fields(product, context):
    """Official own-catalog reads only; never substitute another product or invent attributes."""
    step={'source':'ozon-seller-direct','at':time.time(),'fields':[]}
    keys=context['store']['credentials']
    if not all(keys.get(k) for k in ('client_id','api_key')):
        return product,step|{'reason':'official_credentials_unavailable','fallback':'maozi-direct'}
    if product.get('title') and product.get('image'):
        return product,step|{'reason':'identity_fields_present','fallback':'maozi-direct-for-dossier'}
    try:
        response=await MaoziPublisher(context).seller('/v3/product/info/list',{'sku':[str(product['sku'])]})
        matches=[r for r in response.get('items',[]) if str(r.get('sku'))==str(product['sku'])]
        if len(matches)!=1:return product,step|{'reason':'exact_source_not_in_authorized_catalog','fallback':'maozi-direct'}
        row=matches[0];p=dict(product)
        image=row.get('primary_image') or row.get('images') or []
        if isinstance(image,list):image=image[0] if image else None
        for key,value in (('title',row.get('name')),('image',image)):
            if not p.get(key) and isinstance(value,str) and value:
                p[key]=value;step['fields'].append(key)
        return p,step|{'reason':'exact_source_identity_read','returned_sku':str(row['sku'])}
    except Exception as error:
        return product,step|{'reason':type(error).__name__,'fallback':'maozi-direct'}


async def official_dossier_fields(db, owner, product, review, context):
    """Validate supplementary facts with official schema; never import or set stock."""
    from .. import official_api
    from ..official_publication import LocalPublisher, hydrate_local_dossier
    from ..ozon_direct import dossier
    from ..modules import ModuleError

    keys = context['store']['credentials']
    if not keys.get('client_id') or not keys.get('api_key'):
        return product, ['official_credentials_required']
    profit = review.get('result', {}).get('evidence', {}).get('profit', {})
    candidate = review.get('candidate', {}) | {
        'source_key': str(product['sku']), 'origin': product,
        'title': product.get('title', ''), 'image': product.get('image', ''),
        'price': profit.get('sell_price_cny'),
    }
    packet = context | {'owner': owner, 'candidate': candidate, 'match': review.get('result', {}),
                        'idempotency_key': 'dossier-check-' + str(product['sku'])}
    async with official_api.client(db, keys) as api:
        publisher = LocalPublisher(packet, db, api)
        issues = await hydrate_local_dossier(publisher, db, packet)
        _, missing = dossier(publisher.c)
        if issues or missing:
            return product, list(dict.fromkeys(issues + missing))
        try:
            prepared = await publisher.invoke('prepare')
        except ModuleError as error:
            return product, [str(error)]
        if not prepared.get('ready'):
            return product, prepared.get('missing') or ['official_dossier_invalid']
        return product | {'ozon_dossier': prepared['source_dossier']}, []


class PriceRepairModule:
    name='seed'

    async def run(self, db, owner, sku, seller, *, full_dossier=False):
        from .repair_workflow import enabled, run_one
        if enabled(db):return await run_one(self,db,owner,sku,seller)
        return await self.run_stage(db,owner,sku,seller,full_dossier=full_dossier)

    async def run_stage(self, db, owner, sku, seller, *, full_dossier=False, stage=None, purpose=None):
        official_pending=False
        with db.connect() as c:
            route=c.execute('SELECT * FROM plugin_routes WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            if not route:return {'state':'waiting','reason':'route_missing'}
            store=c.execute('SELECT config,secret FROM stores WHERE owner=? AND id=?',(owner,route['store_id'])).fetchone()
            product=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            row=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_pipeline'").fetchone():
                queued=c.execute('SELECT body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
                full_dossier=full_dossier or bool(queued and json.loads(queued[0]).get('repair_full_dossier'))
                official_pending=bool(queued and json.loads(queued[0]).get('official_dossier_pending'))
        if not store or not product:return {'state':'waiting','reason':'source_or_store_missing'}
        p=json.loads(product[0]);review=json.loads(row[0]) if row else {};before=missing_fields(p)
        # Measurement can proceed without the full publishing dossier.
        can_value=valuation_ready(p)
        require_dossier=(purpose=='publication') if purpose else (full_dossier or review.get('state') in ('matched','needs_review'))
        if can_value and not require_dossier:
            return {'state':'ready','reason':'valuation_inputs_ready','missing_fields':before}
        evidence={'at':time.time(),'before':before,'steps':[]};context={'store':{'id':route['store_id'],'config':json.loads(store['config']),'credentials':db.open(store['secret'])}}
        if stage in (None,'facts'):
            from .repair_facts import supplement
            p=await supplement(db,owner,sku,seller,p,review,context,evidence,require_dossier)
        if stage=='facts':
            evidence['after']=missing_fields(p)
            save_progress(db,owner,sku,seller,p,evidence)
            if purpose=='valuation' and valuation_ready(p):
                return {'state':'ready','reason':'valuation_inputs_ready','missing_fields':evidence['after']}
            return {'state':'progress','reason':'basic_facts_checkpointed','next_stage':'source','missing_fields':evidence['after']}
        acquired=False
        if stage in (None,'source'):
            from .repair_source import supplement
            p,acquired=await supplement(db,owner,sku,seller,p,review,context,evidence,require_dossier,official_pending)
        if stage=='source':
            missing=missing_fields(p);evidence['after']=missing
            save_progress(db,owner,sku,seller,p,evidence)
            if purpose=='valuation' and valuation_ready(p):
                return {'state':'ready','reason':'valuation_inputs_ready','missing_fields':missing}
            failed=any(step.get('source')=='maozi-draft' for step in evidence['steps'])
            if failed or (purpose=='valuation' and not valuation_ready(p)):
                from .repair_retry import classify
                return {'state':'waiting','reason':'source_dossier_pending','failure_class':classify(evidence),'missing_fields':missing}
            return {'state':'progress','reason':'source_dossier_checkpointed','next_stage':'validate','missing_fields':missing}
        missing=missing_fields(p)
        from ..official_publication import backend
        if official_pending or (purpose=='publication' and backend(context['store']['config'])!='maozi'):
            p,official_missing=await official_dossier_fields(db,owner,p,review,context)
            missing=list(dict.fromkeys(missing+official_missing))
            evidence['steps'].append({'source':'official-dossier-validation','missing_fields':official_missing})
        evidence['after']=missing
        save_progress(db,owner,sku,seller,p,evidence)
        reason='missing:'+','.join(missing) if missing else 'complete_dossier'
        if missing and any('采集箱已满' in str((step.get('diagnostic') or {}).get('api_message','')) for step in evidence['steps']):
            reason='maozi_collection_box_full: '+reason
        can_value=valuation_ready(p)
        ready=not missing or (can_value and not require_dossier)
        from .repair_retry import classify
        return {'state':'ready' if ready else 'waiting','reason':'valuation_inputs_ready' if ready and missing else reason,
                'missing_fields':missing,'failure_class':None if ready else classify(evidence)}


def save_progress(db,owner,sku,seller,p,evidence):
    p['collection_evidence']=(p.get('collection_evidence') or {})|{'missing_fields':evidence.get('after',[]),'last_repair':evidence}
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_repair_events(id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,at REAL,body TEXT)')
        c.execute('INSERT INTO plugin_repair_events(owner,sku,seller,at,body) VALUES(?,?,?,?,?)',(owner,sku,seller,time.time(),json.dumps(evidence)))
        SourceLibrary(db).put(owner,p,{'channel':'maozi-field-repair'},connection=c)
