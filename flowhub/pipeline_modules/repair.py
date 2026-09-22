"""Field-specific Maozi ERP supplementation, independent of publication deadlines."""
import asyncio
import json
import time
from ..maozi import MaoziPublisher
from ..source_detail import SourceCollector
from ..modules import Pending
from ..source_library import SourceLibrary
from ..evaluation_requirements import sale_price
from ..plugin_detail import positive
from .pricing import approved_price_intent
from .repair_reads import RepairReads
from .database_work import run as database_work


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


class PriceRepairModule:
    name='seed'

    async def run(self, db, owner, sku, seller, *, full_dossier=False):
        with db.connect() as c:
            route=c.execute('SELECT * FROM plugin_routes WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            if not route:return {'state':'waiting','reason':'route_missing'}
            store=c.execute('SELECT config,secret FROM stores WHERE owner=? AND id=?',(owner,route['store_id'])).fetchone()
            product=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            row=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_pipeline'").fetchone():
                queued=c.execute('SELECT body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
                full_dossier=full_dossier or bool(queued and json.loads(queued[0]).get('repair_full_dossier'))
        if not store or not product:return {'state':'waiting','reason':'source_or_store_missing'}
        p=json.loads(product[0]);review=json.loads(row[0]) if row else {};before=missing_fields(p)
        # Measurement can proceed without the full publishing dossier.
        can_value=valuation_ready(p)
        require_dossier=full_dossier or review.get('state') in ('matched','needs_review')
        if can_value and not require_dossier:
            return {'state':'ready','reason':'valuation_inputs_ready','missing_fields':before}
        evidence={'at':time.time(),'before':before,'steps':[]};context={'store':{'config':json.loads(store['config']),'credentials':db.open(store['secret'])}}
        # Read completed local drafts before spending any remote request or conversion.
        # The collector key binds owner, ERP account and SKU; merge preserves good fields.
        if any(k in before for k in ('weight_g','dimensions_mm','attributes','title','image','url','fresh_dossier')):
            collector=SourceCollector(db,context|{'owner':owner,'candidate':{'source_key':sku}})
            cached=collector.cached_snapshot()
            if cached:
                p=merge_draft(p,cached,now=time.time())
                evidence['steps'].append({'source':'maozi-erp-draft-cache','draft_id':cached.get('draft_id'),
                                          'observed_at':cached['observed_at']})
        if any(k in missing_fields(p) for k in ('title','image')):
            p,direct_step=await direct_identity_fields(p,context)
            evidence['steps'].append(direct_step)
        # Preserve the real approved asking price and its original decision time.
        # This does not claim the old competitor observation has become fresh.
        intent=approved_price_intent(review)
        if 'sale_price' in before and intent:
            p['proposed_sale_price']={'value':intent['value'],'currency':'CNY','observed_at':intent['decided_at'],
                                     'source':'approved-listing-price-intent','source_quote':intent['source_quote']}
            evidence['steps'].append({'source':'approved_listing_intent','decided_at':intent['decided_at']})
        if 'sale_price' in missing_fields(p):
            # Resume an existing campaign's explicit asking-price policy, including
            # RUB storefront prices; use its real admission time, not a fake quote refresh.
            with db.connect() as c:
                campaign=c.execute('SELECT body FROM pipeline_campaigns WHERE owner=? AND enabled=1',(owner,)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_campaigns'").fetchone() else None
                admission=c.execute('SELECT at,body FROM pipeline_admissions WHERE owner=? AND sku=?',(owner,sku)).fetchone() if campaign else None
            if campaign and admission:
                policy=json.loads(campaign[0]);recorded=json.loads(admission['body'])
                from .admission import asking_quote
                quote=asking_quote(p)
                if policy.get('retain_captured_asking_price') and recorded['run_id']==policy['run_id'] and quote and time.time()-admission['at']<21600:
                    p['proposed_sale_price']={**quote,'observed_at':admission['at'],'source':'campaign-asking-price-decision','reference_observed_at':p.get('collected_at'),'run_id':policy['run_id']}
                    evidence['steps'].append({'source':'campaign-asking-price-decision','decided_at':admission['at']})
        from . import direct_facts
        needed=missing_fields(p);direct_enabled=direct_facts.enabled(db)
        # Independent read-only calls overlap; sku3 is only useful for missing price.
        requests={}
        if direct_enabled and any(k in needed for k in ('weight_g','dimensions_mm')):
            requests['category']=('GET','/api.tool/get_category_by_sku',{'params':{'keyword':sku}})
        if 'sale_price' in needed:
            requests['price']=('POST','/api.chrome/sku3',{'params':{'sku':sku},'body':{'sku':sku}})
        async def read(kind,request):
            began=time.monotonic();method,path,kwargs=request
            try:
                response=(await RepairReads.get(MaoziPublisher(context),path,params=kwargs.get('params'))
                          if method=='GET' else await MaoziPublisher(context).erp(method,path,**kwargs))
                return kind,response,{'source':kind,'endpoint':path,'elapsed_ms':round((time.monotonic()-began)*1000),'ok':True}
            except Exception as error:
                return kind,{}, {'source':kind,'endpoint':path,'elapsed_ms':round((time.monotonic()-began)*1000),'reason':type(error).__name__,'ok':False}
        responses={}
        for kind,response,step in await asyncio.gather(*(read(k,v) for k,v in requests.items())):
            responses[kind]=response;evidence['steps'].append(step)
        if 'category' in responses:
            p,step=direct_facts.from_category(p,responses['category'],time.time());evidence['steps'].append(step)
        if 'price' in responses:
            response=responses['price']
            if direct_enabled:
                p,step=direct_facts.from_maozi(p,response,time.time());evidence['steps'].append(step)
            data=response.get('data') or {};flags=response.get('status') or {}
            evidence['steps'].append({'source':'maozi-sku3','status':flags,'returned_sku':data.get('sku'),'seller_id':data.get('sellerId'),'price':data.get('avgPrice')})
            if flags.get('update_sales') is False and str(data.get('sku'))==sku and str(data.get('sellerId'))==seller and positive(data.get('avgPrice')):
                monthly={'observed_at':time.time(),'period':'monthly','average_price_rub':float(data['avgPrice']),
                         'source':'maozi-direct-price-repair'}
                for src,dst in [('salesSchema','sales_schema'),('blockedBySeller','blocked_by_seller'),('category3','category_name'),('brand','brand')]:
                    if data.get(src) is not None:monthly[dst]=data[src]
                p.setdefault('plugin_detail',{}).setdefault('monthly_sales',{})
                p['plugin_detail']['monthly_sales']=(p['plugin_detail']['monthly_sales'] or {})|monthly
        can_value=valuation_ready(p)
        if direct_enabled and (not can_value or require_dossier) and any(k in missing_fields(p) for k in ('weight_g','dimensions_mm','title','image','url')):
            p,step=await direct_facts.public_detail(db,p);evidence['steps'].append(step)
        can_value=valuation_ready(p)
        if (not can_value or require_dossier) and any(k in missing_fields(p) for k in ('weight_g','dimensions_mm','attributes','title','image','url','fresh_dossier')):
            quote=sale_price(p)
            if quote is None:
                # A stored real reference price may seed acquisition metadata only.
                # It never refreshes sale_price evidence or authorizes publication.
                from .admission import asking_quote
                quote=asking_quote(p)
                if quote:
                    evidence['steps'].append({'source':'acquisition_reference_only',
                        'value':quote['value'],'currency':quote['currency'],
                        'observed_at':p.get('collected_at'),'usable_for_valuation':False})
            draft_price=intent['value'] if intent else None
            if draft_price is None and quote:
                if quote['currency']=='CNY':draft_price=quote['value']
                else:
                    try:
                        exchange=await RepairReads.get(MaoziPublisher(context),'/api.exchange_rate/index')
                    except Exception as error:
                        evidence['steps'].append({'source':'maozi-RUBCNY','reason':type(error).__name__})
                        exchange={}
                    rate=positive((exchange.get('RUBCNY') or {}).get('value'))
                    if rate:
                        draft_price=round(quote['value']*rate,2)
                        evidence['steps'].append({'source':'maozi-RUBCNY','rate':rate,'at':time.time()})
            draft_context=context|{'owner':owner,'existing_favorite_only':draft_price is None,
                'candidate':{'source_key':sku,'title':p.get('title',''),'image':p.get('image',''),'price':draft_price}}
            try:
                snapshot=await SourceCollector(db,draft_context).collect()
                from ..cluster_compute import remote
                packet=await remote('dossier',{'sku':str(p['sku']),'snapshot':snapshot})
                p=merge_draft(p,snapshot,now=time.time(),packet=packet)
                evidence['steps'].append({'source':'maozi-erp-draft','draft_id':snapshot.get('draft_id'),'observed_at':snapshot['observed_at']})
            except Pending as error:
                evidence['steps'].append({'source':'maozi-draft','reason':str(error)})
            except Exception as error:
                evidence['steps'].append({'source':'maozi-draft','reason':str(error) if getattr(error,'diagnostic',None) is not None else type(error).__name__,'diagnostic':getattr(error,'diagnostic',{})})
        missing=missing_fields(p);evidence['after']=missing
        await database_work(save_progress,db,owner,sku,seller,p,evidence)
        reason='missing:'+','.join(missing) if missing else 'complete_dossier'
        if missing and any('采集箱已满' in str((step.get('diagnostic') or {}).get('api_message','')) for step in evidence['steps']):
            reason='maozi_collection_box_full: '+reason
        can_value=valuation_ready(p)
        ready=not missing or (can_value and not require_dossier)
        from .repair_retry import classify
        return {'state':'ready' if ready else 'waiting','reason':'valuation_inputs_ready' if ready and missing else reason,
                'missing_fields':missing,'failure_class':None if ready else classify(evidence)}


def save_progress(db,owner,sku,seller,p,evidence):
    missing=evidence['after']
    p['collection_evidence']=(p.get('collection_evidence') or {})|{'missing_fields':missing,'last_repair':evidence}
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_repair_events(id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,at REAL,body TEXT)')
        c.execute('INSERT INTO plugin_repair_events(owner,sku,seller,at,body) VALUES(?,?,?,?,?)',(owner,sku,seller,time.time(),json.dumps(evidence)))
        SourceLibrary(db).put(owner,p,{'channel':'maozi-field-repair'},connection=c)
