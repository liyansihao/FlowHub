// compareBot supplier binding with existing ERP product, FBS and profit verification.
import fs from 'node:fs/promises';
import {cachedCategory} from './category-cache.mjs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const root=path.resolve(process.env.FLOWEF_LEGACY_ROOT||path.join(import.meta.dirname,'../..'));
const load=p=>import(pathToFileURL(path.join(root,p)).href);
let inputText=''; for await (const chunk of process.stdin) inputText+=chunk;
const input=JSON.parse(inputText);
if(input.action!=='comparebot_evaluate')throw Error('compareBot evaluation only');
const read=p=>fs.readFile(path.join(root,p),'utf8').then(JSON.parse);
const {resolveConfiguredMaoziToken}=await load('ozon-runtime/lib/maozi-credentials.mjs');
const {createGloballyPacedMaoziTransport}=await load('ozon-runtime/lib/maozi-transport.mjs');
let diagnosticStage='initialize';
const stageDurations={};let stageBegan=Date.now();
function stage(name){stageDurations[diagnosticStage]=(stageDurations[diagnosticStage]||0)+Date.now()-stageBegan;diagnosticStage=name;stageBegan=Date.now();}
const requestTrace=[];
const originalFetch=globalThis.fetch;
globalThis.fetch=async function(resource, options){
 const began=Date.now();
 let target='unknown';
 try{const u=new URL(typeof resource==='string'||resource instanceof URL?resource:resource.url);target=u.origin+u.pathname;}catch{}
 try{return await originalFetch(resource,options);}
 catch(error){
  const codes=[error.cause?.code,...(error.cause?.errors||[]).map(e=>e.code)].filter(Boolean);
  error.networkDiagnostic={stage:diagnosticStage,target,codes,elapsed_ms:Date.now()-began};
  throw error;
 }finally{requestTrace.push({stage:diagnosticStage,target,elapsed_ms:Date.now()-began});if(requestTrace.length>12)requestTrace.shift();}
};
const transport=createGloballyPacedMaoziTransport({token:await resolveConfiguredMaoziToken(),env:{...process.env,MAOZI_HTTP_BACKEND:'native'},allowWrites:false,apiIntervalMs:2500,reserveFutureSlot:true,maxWaitMs:60000,requestTimeoutMs:30000});
async function main(){
 const engine=await load('maozi_direct_new_method/maozi_new_method_direct.mjs');
 const product=input.product;
 if(!product?.sku||!product.expansion_source)throw Error('fresh expansion provenance required');
 const feedback=await read('maozi_direct_new_method/state/feedback/match-feedback.json');
 const {feedbackBlockForProduct,feedbackBlockForPair}=await load('maozi_direct_new_method/lib/match-feedback.mjs');
 const rules=await read('maozi_direct_new_method/prohibited-categories.json');
 const cfg=await read('flow_b_ef/state/config.json');
 const {blockedImportBrand}=await load('flow_ef_category_fbs/lib/brand-import-conflict.mjs');
 const category=engine.categoryPolicyFor(product);
 const plugin=product.plugin_detail, monthly=plugin?.monthly_sales;
 const {verifiedPluginSource,verifiedPluginEvaluation}=await load('ozon-runtime/lib/plugin-source-policy.mjs');
 const explicitPluginSource=verifiedPluginSource(product);
 const q=product.price_evidence;
 const weightFirst=product.weight_first_valuation===true && product.profit_evaluation_only===true
  && Number(product.valuation_weight_g)>0 && Number.isFinite(Number(product.valuation_weight_g))
  && String(plugin?.sku||product.direct_source_facts?.sku)===String(product.sku)
  && String(product.source_relation?.seller_id)===String(product.seller_id)
  && product.expansion_source?.contract==='flowhub-same-seller-v1' && product.expansion_source.seed_bindings?.length>0
  && ['CNY','RUB'].includes(q?.currency) && q.value>0 && Number.isFinite(q.value)
  && Number.isFinite(q.observed_at) && Date.now()/1000-q.observed_at>=0 && Date.now()/1000-q.observed_at<21600;
 const evaluationOnly=weightFirst||verifiedPluginEvaluation(product);
 // Priority lists select ranking discoveries; explicitly supplied, verified plugin
 // candidates need not belong to a ranking-priority list. Hard exclusions remain.
 if(category.holdout||engine.prohibitedCategoryMatch(product,rules)||blockedImportBrand(product,cfg.flow_f.blocked_source_brands)||feedbackBlockForProduct(product,feedback).blocked)return {rejected:'protected_product'};
 if(!category.eligible&&!explicitPluginSource&&!evaluationOnly)return {rejected:'category_not_prioritized'};
 const {createMaoziClient}=await load('maozi_direct_new_method/lib/portable-support/maozi-client.mjs');
 const client=createMaoziClient({transport});
 stage('exchange_rate');
 const {cachedExchangeRate}=await load('FlowEF-production/bridges/exchange-rate.mjs');
 const exchange=await cachedExchangeRate(path.join(root,'FlowEF-production/state/production/cache'),transport);
 const rubCny=exchange.value;
 const source=input.source;
 if(!source)throw Error('source evidence missing');
 if(input.action==='comparebot_evaluate'){
  const d=source.comparebot?.decision;
  const rows=source.comparebot?.search_and_rank?.candidates||[];
  const review=d?.qwen_review||{};
  const row=rows.find(r=>String(r.candidate.offer_id)===String(d?.selected_offer_id));
  const provisional=source.evaluation_only===true&&d?.outcome==='manual_review';
  const website=product.website_listing_authorization;
  const humanConfirmed=website?.same_product_confirmed===true&&String(website.sku)===String(product.sku)&&typeof website.id==='string'&&website.id.length>10&&Date.now()/1000-website.at>=0&&Date.now()/1000-website.at<86400&&d?.human_review?.actor&&!review.brand_or_model_conflict;
  const qualified=humanConfirmed||d?.outcome==='approved'&&!review.brand_or_model_conflict&&((Number(row?.dinov2_similarity)>=0.86&&source.comparebot.search_and_rank.query.size==='small')||(Number(row?.dinov2_similarity)>=0.82&&review.verdict==='match'));
  if((!provisional&&!qualified)||!row||!Number.isFinite(Number(row.dinov2_similarity))||Number(row.dinov2_similarity)<0.63||Number(row.dinov2_similarity)>1||String(source.selected_offer_id)!==String(d.selected_offer_id)
   ||!/^\d+$/.test(String(source.selected_offer_id))
   ||source.selected_offer_url!==`https://detail.1688.com/offer/${source.selected_offer_id}.html`
   ||!(Number(source.selected_cost_cny)>0)||!Number.isFinite(Number(source.selected_cost_cny))
   ||Number(source.selected_cost_cny)!==Number(row.candidate.price_cny)
   ||source.selected_image_url!==row.candidate.image_url)throw Error('invalid compareBot source binding');
 }
 if(feedbackBlockForPair(product,source,feedback).blocked)return {rejected:'human_mismatch'};
 const useLocalProfit=weightFirst||await fs.access(path.join(root,'FlowEF-production/state/production/local-profit.enabled')).then(()=>true).catch(()=>false);
 stage('commission_and_category');
 const savedCategory=cachedCategory(product);
 const categoryRequest=savedCategory?Promise.resolve(savedCategory):client.getCategoryBySku(product.sku);
 const [commissions,categoryData]=await Promise.all([
  useLocalProfit?Promise.resolve(null):client.listCategoryCommissions(),
  categoryRequest.catch(error=>{
   if(!weightFirst)throw error;
   return {cate:[null,null,product.category_id||null],product_info:{},lookup_unavailable:true};
  }),
 ]);
 if(engine.prohibitedLeafCategoryMatch(categoryData))return {rejected:'prohibited_leaf'};
 stage('fbs_verification');
 let fbs=evaluationOnly?{verified:false,source:'deferred_until_publication',raw_mode:monthly?.sales_schema??null}:await engine.observePureFbs(transport,product,'flowef-production-decision');
 if(!fbs.verified&&(fbs.raw_mode===null||fbs.raw_mode===undefined||fbs.raw_mode==='')&&explicitPluginSource&&Date.now()/1000-monthly.observed_at<900){
  fbs={...fbs,verified:true,modes:['FBS'],raw_mode:'FBS',source:'plugin-seller-sku-sales',observed_at:new Date(monthly.observed_at*1000).toISOString(),cache_observation:fbs};
 }
 if(!fbs.verified&&!evaluationOnly)return {rejected:'source_unverified'};
 const evaluatedSell=evaluationOnly?Math.round(product.price_evidence.value*(product.price_evidence.currency==='CNY'?1:rubCny)*100)/100:undefined;
 stage('postal_profit');
 let profit;
 if(useLocalProfit){
  const {localProfit}=await load('FlowEF-production/bridges/local-profit.mjs');
  profit=await localProfit(root,{product,category_data:categoryData,source,rub_cny:rubCny,sell_cny:evaluatedSell??engine.productSalePriceCny(product,rubCny)});
  if(profit.rejected==='no_logistics_route')throw Error('No eligible logistics route for ChinaPost');
  if(profit.rejected)throw Error('Local official commission or cost inputs unavailable');
 }else profit=await engine.maoziProfit({client,commissions,rubCny,product,categoryData,purchasePrice:source.selected_cost_cny,sellPriceCny:evaluatedSell});
 if(profit.input.logistics!=='ChinaPost')throw Error('postal route required');
 return {source,profit,fbs,product,evaluation_only_verified:evaluationOnly,exchange_rate_evidence:exchange,observed_at:new Date().toISOString()};
}
try{const result=await main();stage('finished');if(input.action==='comparebot_evaluate')await fs.appendFile(path.join(root,'FlowEF-production/state/production/request-diagnostics.jsonl'),JSON.stringify({at:new Date().toISOString(),action:input.action,sku:input.product?.sku,ok:true,rejected:result.rejected||null,stage_ms:stageDurations,requests:requestTrace})+'\n',{mode:0o600}).catch(()=>{});process.stdout.write(JSON.stringify({ok:true,result}));}
catch(error){await fs.appendFile(path.join(root,'FlowEF-production/state/production/request-diagnostics.jsonl'),JSON.stringify({at:new Date().toISOString(),action:input.action,sku:input.product?.sku,diagnostic:error.networkDiagnostic||{stage:diagnosticStage,recent_requests:requestTrace}})+'\n',{mode:0o600}).catch(()=>{});process.stdout.write(JSON.stringify({ok:false,error:{code:error.code||error.name||'Error',message:String(error.message).replace(/Bearer\s+\S+/gi,'Bearer [redacted]').slice(0,700),diagnostic:error.networkDiagnostic||{stage:diagnosticStage,recent_requests:requestTrace},status:error.status,unknown:error.writeOutcomeUnknown===true,retry_after_ms:error.retryAfterMs}}));process.exitCode=1;}
