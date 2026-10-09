// FlowHub-owned policy, based on ozon-runtime/lib/plugin-source-policy.mjs.
// Explicit SKU sources are admitted separately from ranking-discovery priorities.
// Callers must still enforce category holdouts, prohibited categories and feedback.
export function supportedSourceModes(value){
 const modes=Array.isArray(value)?value:typeof value==='string'?value.split(','):[];
 return modes.length>0&&modes.every(mode=>['FBO','FBS'].includes(String(mode).trim().toUpperCase()));
}
const STATIC_DOSSIER_MAX_AGE = 7 * 86400;
function validDossier(p, now, maxAge=21600){
 if(['maozi-plugin-sku-detail-v1','maozi-erp-draft-detail-v1'].includes(p?.contract))return true;
 if(p?.contract!=='direct-field-dossier-v1')return false;
 return Number.isFinite(p.weight_g)&&p.weight_g>0
  &&Array.isArray(p.dimensions_mm)&&p.dimensions_mm.length===3&&p.dimensions_mm.every(v=>Number.isFinite(v)&&v>0)
  &&Array.isArray(p.attributes)&&p.attributes.length>0
  &&['weight_g','dimensions_mm','attributes'].every(k=>{
   const o=p.field_observations?.[k];
   return o&&String(o.sku)===String(p.sku)&&['maozi-erp-draft','maozi-category-by-sku','ozon-product-page'].includes(o.source)
    &&Number.isFinite(o.observed_at)&&o.observed_at>0&&now-o.observed_at>=0&&now-o.observed_at<maxAge;
  });
}
export function verifiedPluginEvaluation(product, now=Date.now()/1000){
 const p=product?.plugin_detail,q=product?.price_evidence;
 return product?.profit_evaluation_only===true&&validDossier(p,now)
  &&String(p.sku)===String(product.sku)
  &&String(product.source_relation?.seller_id)===String(product.seller_id)
  &&product.expansion_source?.contract==='flowhub-same-seller-v1'&&product.expansion_source.seed_bindings?.length>0
  &&[p.observed_at,q?.observed_at].every(t=>Number.isFinite(t)&&now-t>=0&&now-t<21600)
  &&['CNY','RUB'].includes(q?.currency)&&Number.isFinite(q.value)&&q.value>0;
}
export function verifiedPluginSource(product, now=Date.now()/1000){
 const plugin=product?.plugin_detail, monthly=plugin?.monthly_sales;
 return validDossier(plugin,now)
  && String(plugin.sku)===String(product.sku)
  && String(product.source_relation?.seller_id)===String(product.seller_id)
  && product.expansion_source?.contract==='flowhub-same-seller-v1'
  && product.expansion_source.seed_bindings?.length>0
  && [plugin.observed_at,monthly?.observed_at].every(t=>Number.isFinite(t)&&now-t>=0&&now-t<21600)
  && supportedSourceModes(monthly?.sales_schema)&&monthly?.blocked_by_seller===false;
}

// This flag is injected only from a scoped, unexpired user publication permission.
export function verifiedPluginPublication(product, now=Date.now()/1000){
 const monthly=product?.plugin_detail?.monthly_sales;
 const intent=product?.approved_listing_price;
 const explicit=intent?.kind==='approved_listing_price'&&String(intent.sku)===String(product.sku)
  &&intent.currency==='CNY'&&Number.isFinite(intent.value)&&intent.value>0
  &&Number.isFinite(intent.decided_at)&&now-intent.decided_at>=0&&now-intent.decided_at<21600;
 const valuation=explicit ? {...product,price_evidence:{value:intent.value,currency:intent.currency,observed_at:intent.decided_at}} : product;
 const website=product?.website_listing_authorization;
 const d=product?.plugin_detail;
 // Static packaging/attribute evidence does not expire at the price refresh
 // interval. The publisher separately checks the latest SKU packet for changes.
 // This route still needs an unexpired explicit price and publication permission.
 const stableSource=explicit&&product?.profit_evaluation_only===true
  &&validDossier(d,now,STATIC_DOSSIER_MAX_AGE)
  &&Number.isFinite(d?.observed_at)&&d.observed_at>0&&now-d.observed_at>=0&&now-d.observed_at<STATIC_DOSSIER_MAX_AGE
  &&String(d.sku)===String(product.sku)&&String(product.source_relation?.seller_id)===String(product.seller_id)
  &&product.expansion_source?.contract==='flowhub-same-seller-v1'&&product.expansion_source.seed_bindings?.length>0
  &&Number.isFinite(d.weight_g)&&d.weight_g>0&&Array.isArray(d.dimensions_mm)&&d.dimensions_mm.length===3&&d.dimensions_mm.every(v=>Number.isFinite(v)&&v>0)
  &&Array.isArray(d.attributes)&&d.attributes.length>0;
 const websiteSource=website?.same_product_confirmed===true&&typeof website.id==='string'&&website.id.length>10
  &&String(website.sku)===String(product.sku)&&Number.isFinite(website.at)&&now-website.at>=0&&now-website.at<86400
  &&String(d?.sku)===String(product.sku)&&String(product.source_relation?.seller_id)===String(product.seller_id)
  &&product.expansion_source?.contract==='flowhub-same-seller-v1'&&product.expansion_source.seed_bindings?.length>0
  &&Number.isFinite(d.weight_g)&&d.weight_g>0&&Array.isArray(d.dimensions_mm)&&d.dimensions_mm.length===3&&d.dimensions_mm.every(v=>Number.isFinite(v)&&v>0)
  &&explicit;
 return product?.allow_unknown_publication===true && (websiteSource||stableSource||verifiedPluginEvaluation(valuation,now))
  && ([undefined,null,''].includes(monthly?.sales_schema)||supportedSourceModes(monthly?.sales_schema))
  && [undefined,null,false].includes(monthly?.blocked_by_seller);
}
