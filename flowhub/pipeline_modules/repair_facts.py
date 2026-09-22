"""Independent basic-fact reads; no draft creation or publication."""
import asyncio
import json
import time

async def supplement(db,owner,sku,seller,p,review,context,evidence,require_dossier):
    from .repair import missing_fields, valuation_ready, merge_draft, direct_identity_fields
    from .pricing import approved_price_intent
    from .repair_reads import RepairReads
    from ..maozi import MaoziPublisher
    from ..source_detail import SourceCollector
    from ..plugin_detail import positive
    before=missing_fields(p)
    intent=approved_price_intent(review)
    can_value=valuation_ready(p)
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
    return p
