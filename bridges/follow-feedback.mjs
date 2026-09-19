// Read-only publication feedback. Reuses the same exclusions as comparebot.mjs;
// full catalog attributes are not part of the ERP follow-import contract.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

export function checkFollowFeedback(product, source, deps, now=Date.now()/1000) {
  const {engine, rules, cfg, feedback, feedbackBlockForProduct, feedbackBlockForPair, blockedImportBrand,
    verifiedPluginSource, verifiedPluginEvaluation} = deps;
  if (!product?.sku || !product.expansion_source || source?.comparebot?.decision?.outcome !== 'approved')
    throw Error('approved source evidence required');
  const intent=product.approved_listing_price;
  const approvedPrice=intent?.kind==='approved_listing_price' && String(intent.sku)===String(product.sku)
    && intent.currency==='CNY' && Number.isFinite(intent.value) && intent.value>0
    && Number.isFinite(intent.decided_at) && now-intent.decided_at>=0 && now-intent.decided_at<21600;
  const q=approvedPrice ? {value:intent.value,currency:'CNY',observed_at:intent.decided_at} : product.price_evidence;
  const weightFirst=product.weight_first_valuation===true && product.profit_evaluation_only===true
    && Number.isFinite(Number(product.valuation_weight_g)) && Number(product.valuation_weight_g)>0
    && String(product.plugin_detail?.sku||product.direct_source_facts?.sku)===String(product.sku)
    && String(product.source_relation?.seller_id)===String(product.seller_id)
    && product.expansion_source.contract==='flowhub-same-seller-v1' && product.expansion_source.seed_bindings?.length>0
    && ['CNY','RUB'].includes(q?.currency) && Number.isFinite(q.value) && q.value>0
    && Number.isFinite(q.observed_at) && now-q.observed_at>=0 && now-q.observed_at<21600;
  const category=engine.categoryPolicyFor(product);
  if (category.holdout || engine.prohibitedCategoryMatch(product,rules)
      || blockedImportBrand(product,cfg.flow_f.blocked_source_brands)
      || feedbackBlockForProduct(product,feedback).blocked) return {rejected:'protected_product'};
  if (!category.eligible && !weightFirst && !verifiedPluginSource(product) && !verifiedPluginEvaluation(product))
    return {rejected:'category_not_prioritized'};
  return {blocked:Boolean(feedbackBlockForPair(product,source,feedback).blocked)};
}

if (process.argv[1] && import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    let text='';for await (const chunk of process.stdin) text+=chunk;
    const input=JSON.parse(text), root=path.resolve(process.env.FLOWEF_LEGACY_ROOT);
    const load=p=>import(pathToFileURL(path.join(root,p)).href);
    const read=p=>fs.readFile(path.join(root,p),'utf8').then(JSON.parse);
    const deps={engine:await load('maozi_direct_new_method/maozi_new_method_direct.mjs'),
      rules:await read('maozi_direct_new_method/prohibited-categories.json'),
      cfg:await read('flow_b_ef/state/config.json'),
      feedback:await read('maozi_direct_new_method/state/feedback/match-feedback.json'),
      ...await load('maozi_direct_new_method/lib/match-feedback.mjs'),
      ...await load('flow_ef_category_fbs/lib/brand-import-conflict.mjs'),
      ...await load('ozon-runtime/lib/plugin-source-policy.mjs')};
    process.stdout.write(JSON.stringify({ok:true,result:checkFollowFeedback(input.product,input.source,deps)}));
  } catch { process.stdout.write(JSON.stringify({ok:false,error:'follow_feedback_unavailable'})); process.exitCode=1; }
}
