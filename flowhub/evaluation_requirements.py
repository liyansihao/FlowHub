"""Separate usable valuation inputs from publication eligibility."""
import re,time
from .plugin_detail import positive


def review_sale_price(product,now=None):
 """Available asking quote: minimum follow price, then ordinary asking price.

 Historical observations are allowed for review per owner policy. Keep their
 actual timestamp; publication freshness checks remain a separate concern.
 Monthly sales averages and procurement costs are not asking prices.
 """
 now=time.time() if now is None else now
 def quote(raw,source,kind):
  if not isinstance(raw,dict):return None
  value=positive(raw.get('value'));stamp=raw.get('observed_at')
  if not value or raw.get('currency') not in ('CNY','RUB'):return None
  if not isinstance(stamp,(int,float)) or stamp<=0 or stamp>now:return None
  return dict(raw,value=value,source=raw.get('source') or source,kind=kind,
              historical=now-stamp>=21600)
 detail=product.get('plugin_detail') or {}
 for raw in (product.get('minimum_follow_price'),detail.get('minimum_follow_price')):
  found=quote(raw,'maozi-minimum-follow-price','minimum_follow_price')
  if found:return found
 # This field is written only from a bound, observed follow-seller offer list.
 for raw in (product.get('ordinary_sale_price'),product.get('proposed_sale_price')):
  found=quote(raw,'observed-asking-price','ordinary_sale_price')
  if found:return found
 stamp=product.get('collected_at')
 relation=product.get('source_relation') or {}
 bound=relation.get('seller_id')==product.get('seller_id') and relation.get('root_seeds')
 if bound:
  found=quote({'value':product.get('current_price_rub'),'currency':'RUB','observed_at':stamp},'ozon-storefront-price','ordinary_sale_price')
  if found:return found
  text=str(product.get('current_price_display') or '').replace('\\u2009','').replace('\u2009','').strip()
  match=re.fullmatch(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*[¥￥]',text)
  if match:
   found=quote({'value':match[1].replace(',','.'),'currency':'CNY','observed_at':stamp,'raw':product['current_price_display']},'ozon-china-storefront-display','ordinary_sale_price')
   if found:return found
 # Previous collection repair retained a real asking price with its original
 # observation time. It was excluded solely by the former freshness policy.
 steps=((product.get('collection_evidence') or {}).get('last_repair') or {}).get('steps') or []
 for step in steps:
  if step.get('source')=='acquisition_reference_only':
   found=quote(step,'acquisition_reference_only','ordinary_sale_price')
   if found:return found
 return None


def sale_price(product,now=None):
 now=time.time() if now is None else now
 sales=product.get('plugin_detail',{}).get('monthly_sales') or {}
 value=positive(sales.get('average_price_rub'))
 if value and 0<=now-sales.get('observed_at',0)<21600:
  return {'value':value,'currency':'RUB','period':'monthly','source':'maozi-plugin','observed_at':sales['observed_at']}
 quote=product.get('proposed_sale_price') or {}
 if quote.get('currency') in ('CNY','RUB') and positive(quote.get('value')) and 0<=now-quote.get('observed_at',0)<21600:
  return dict(quote,source=quote.get('source','proposed_sale_price'))
 # Existing Ozon China storefront capture retained its rendered CNY price.
 text=str(product.get('current_price_display') or '').replace('\\u2009','').replace('\u2009','').strip()
 match=re.fullmatch(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*[¥￥]',text)
 relation=product.get('source_relation') or {}
 if match and relation.get('seller_id')==product.get('seller_id') and relation.get('root_seeds') and 0<=now-product.get('collected_at',0)<21600:
  value=positive(match[1].replace(',','.'))
  if value:return {'value':value,'currency':'CNY','period':'current_display','source':'ozon-china-storefront-display','observed_at':product['collected_at'],'raw':product['current_price_display']}
 return None


def publication_blockers(product,now=None):
 now=time.time() if now is None else now
 d=product.get('plugin_detail') or {};sales=d.get('monthly_sales') or {};result=[]
 fresh=0<=now-sales.get('observed_at',0)<900
 if not fresh or sales.get('sales_schema')!='FBS':result.append('fresh_pure_fbs_required')
 if not fresh or sales.get('blocked_by_seller') is not False:result.append('follow_permission_unverified_or_blocked')
 if not d.get('attributes'):result.append('publication_attributes_missing')
 return result
