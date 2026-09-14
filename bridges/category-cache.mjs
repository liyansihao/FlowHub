// Reuse only a fresh, exact-SKU response from the verified Maozi category endpoint.
export function cachedCategory(product, now=Date.now()/1000) {
 const facts=product.direct_source_facts;
 const data=facts?.category_response;
 if(facts?.source!=='maozi-category-by-sku'||String(facts.sku)!==String(product.sku)
    ||String(data?.sku)!==String(product.sku)||!Number.isFinite(facts.observed_at)
    ||now-facts.observed_at<0||now-facts.observed_at>=21600)return null;
 if(!Array.isArray(data.cate)||data.cate.length<3||!data.cate[2])return null;
 return data;
}
