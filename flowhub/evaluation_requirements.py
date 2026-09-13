"""Separate usable valuation inputs from publication eligibility."""
import re,time
from .plugin_detail import positive


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
