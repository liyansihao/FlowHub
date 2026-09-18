"""One source-dossier acquisition stage using the existing durable write journal."""
import time

async def supplement(db,owner,sku,seller,p,review,context,evidence,require_dossier,official_pending):
    from .repair import missing_fields, valuation_ready, merge_draft
    from .pricing import approved_price_intent
    from .repair_reads import RepairReads
    from ..maozi import MaoziPublisher
    from ..source_detail import SourceCollector
    from ..evaluation_requirements import sale_price
    from ..plugin_detail import positive
    from ..modules import Pending
    intent=approved_price_intent(review)
    can_value=valuation_ready(p)
    acquired=False
    if ((not can_value or require_dossier) and any(k in missing_fields(p) for k in ('weight_g','dimensions_mm','attributes','title','image','url','fresh_dossier'))) or (official_pending and not p.get('ozon_dossier',{}).get('attributes')):
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
            acquired=True
            evidence['steps'].append({'source':'maozi-erp-draft','draft_id':snapshot.get('draft_id'),'observed_at':snapshot['observed_at']})
        except Pending as error:
            evidence['steps'].append({'source':'maozi-draft','reason':str(error)})
        except Exception as error:
            evidence['steps'].append({'source':'maozi-draft','reason':str(error) if getattr(error,'diagnostic',None) is not None else type(error).__name__,'diagnostic':getattr(error,'diagnostic',{})})
    return p,acquired
