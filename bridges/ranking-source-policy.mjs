// A distinct, SKU/seller-bound demand source; never fabricate own-shop root bindings.
export function verifiedRankingSource(product, now=Date.now()/1000) {
  const source=product?.expansion_source, e=source?.ranking;
  if(source?.contract!=='flowhub-sales-ranking-v1' || source.coverage!=='sales-ranking'
      || e?.contract!=='flowhub-sales-ranking-v1' || !e.evidence_hash
      || e.endpoint!=='/api.selection.top/lists' || e.period!=='28d'
      || String(e.sku)!==String(product.sku) || String(e.seller_id)!==String(product.seller_id)
      || typeof e.sold_count!=='number' || !Number.isFinite(e.sold_count) || e.sold_count<=0
      || typeof e.observed_at!=='number' || !Number.isFinite(e.observed_at)) return false;
  let updated=e.observed_at;
  if(e.provider_updated_at!==undefined && e.provider_updated_at!==null && e.provider_updated_at!=='') {
    const value=e.provider_updated_at;
    updated=Number(value);
    if(!Number.isFinite(updated)) updated=Date.parse(value)/1000;
    else if(updated>1e12) updated/=1000;
  }
  return now-e.observed_at>=0 && now-e.observed_at<604800
    && Number.isFinite(updated) && now-updated>=0 && now-updated<604800;
}
