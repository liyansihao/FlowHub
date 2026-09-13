"""Field-specific Maozi ERP supplementation, independent of publication deadlines."""
import json
import time
from ..maozi import MaoziPublisher
from ..source_detail import SourceCollector
from ..modules import Pending
from ..source_library import SourceLibrary
from ..evaluation_requirements import sale_price
from ..plugin_detail import positive
from .pricing import approved_price_intent


def missing_fields(product):
    detail=product.get('plugin_detail') or {};missing=[]
    for key in ('title','image','url'):
        if not product.get(key):missing.append(key)
    if not positive(detail.get('weight_g')):missing.append('weight_g')
    dims=detail.get('dimensions_mm') or []
    if len(dims)!=3 or not all(positive(v) for v in dims):missing.append('dimensions_mm')
    if not detail.get('attributes'):missing.append('attributes')
    observed=detail.get('observed_at')
    if not isinstance(observed,(int,float)) or not 0<=time.time()-observed<21600:missing.append('fresh_dossier')
    if sale_price(product) is None:missing.append('sale_price')
    return missing


def merge_draft(product, snapshot):
    if str(snapshot.get('source_key'))!=str(product['sku']):raise ValueError('draft_source_mismatch')
    detail=snapshot.get('detail') or {}
    if len(detail.get('skus') or [])!=1:raise ValueError('ambiguous_draft_variant')
    p=json.loads(json.dumps(product));old=p.get('plugin_detail') or {}
    fields={'weight_g':detail.get('package_weight'),
            'dimensions_mm':[detail.get(k) for k in ('package_length','package_width','package_height')],
            'attributes':detail.get('common_attributes')}
    # Preserve each independently observed field even if another field is absent.
    merged=dict(old);observed_fields=[]
    for key,value in fields.items():
        valid=(bool(value) if key=='attributes' else
               len(value)==3 and all(positive(v) for v in value) if key=='dimensions_mm' else positive(value))
        if valid:
            merged[key]=value;observed_fields.append(key)
    merged.setdefault('field_observations',{}).update({key:{'source':'maozi-erp-draft',
        'observed_at':snapshot['observed_at'],'draft_id':snapshot.get('draft_id')} for key in observed_fields})
    if len(observed_fields)==3:
        merged.update(contract='maozi-erp-draft-detail-v1',sku=str(p['sku']),
                      observed_at=snapshot['observed_at'],erp_draft_id=snapshot.get('draft_id'))
        categories=detail.get('category_id') or []
        if len(categories)>1:merged['description_category_id']=categories[1]
    p['plugin_detail']=merged
    if detail.get('title'):p['title']=detail['title']
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

    async def run(self, db, owner, sku, seller):
        with db.connect() as c:
            route=c.execute('SELECT * FROM plugin_routes WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            if not route:return {'state':'waiting','reason':'route_missing'}
            store=c.execute('SELECT config,secret FROM stores WHERE owner=? AND id=?',(owner,route['store_id'])).fetchone()
            product=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            row=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        if not store or not product:return {'state':'waiting','reason':'source_or_store_missing'}
        p=json.loads(product[0]);review=json.loads(row[0]) if row else {};before=missing_fields(p)
        evidence={'at':time.time(),'before':before,'steps':[]};context={'store':{'config':json.loads(store['config']),'credentials':db.open(store['secret'])}}
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
        needed=missing_fields(p)
        if 'sale_price' in needed:
            try:
                response=await MaoziPublisher(context).erp('POST','/api.chrome/sku3',params={'sku':sku},body={'sku':sku})
            except Exception as error:
                evidence['steps'].append({'source':'maozi-sku3','reason':type(error).__name__})
                response={}
            data=response.get('data') or {};flags=response.get('status') or {}
            evidence['steps'].append({'source':'maozi-sku3','status':flags,'returned_sku':data.get('sku'),'seller_id':data.get('sellerId'),'price':data.get('avgPrice')})
            if flags.get('update_sales') is False and str(data.get('sku'))==sku and str(data.get('sellerId'))==seller and positive(data.get('avgPrice')):
                monthly={'observed_at':time.time(),'period':'monthly','average_price_rub':float(data['avgPrice']),
                         'source':'maozi-direct-price-repair'}
                for src,dst in [('salesSchema','sales_schema'),('blockedBySeller','blocked_by_seller'),('category3','category_name'),('brand','brand')]:
                    if data.get(src) is not None:monthly[dst]=data[src]
                p.setdefault('plugin_detail',{}).setdefault('monthly_sales',{})
                p['plugin_detail']['monthly_sales']=(p['plugin_detail']['monthly_sales'] or {})|monthly
        if any(k in missing_fields(p) for k in ('weight_g','dimensions_mm','attributes','title','image','url','fresh_dossier')):
            quote=sale_price(p)
            draft_price=intent['value'] if intent else None
            if draft_price is None and quote:
                if quote['currency']=='CNY':draft_price=quote['value']
                else:
                    try:
                        exchange=await MaoziPublisher(context).erp('GET','/api.exchange_rate/index')
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
                p=merge_draft(p,snapshot)
                evidence['steps'].append({'source':'maozi-erp-draft','draft_id':snapshot.get('draft_id'),'observed_at':snapshot['observed_at']})
            except Pending as error:
                evidence['steps'].append({'source':'maozi-draft','reason':str(error)})
            except Exception as error:
                evidence['steps'].append({'source':'maozi-draft','reason':str(error) if getattr(error,'diagnostic',None) is not None else type(error).__name__,'diagnostic':getattr(error,'diagnostic',{})})
        missing=missing_fields(p);evidence['after']=missing
        p['collection_evidence']=(p.get('collection_evidence') or {})|{'missing_fields':missing,'last_repair':evidence}
        with db.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS plugin_repair_events(id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,at REAL,body TEXT)')
            c.execute('INSERT INTO plugin_repair_events(owner,sku,seller,at,body) VALUES(?,?,?,?,?)',(owner,sku,seller,time.time(),json.dumps(evidence)))
            SourceLibrary(db).put(owner,p,{'channel':'maozi-field-repair'},connection=c)
        return {'state':'waiting' if missing else 'ready','reason':'missing:'+','.join(missing) if missing else 'complete_dossier','missing_fields':missing}
