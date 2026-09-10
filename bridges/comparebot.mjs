// compareBot supplier binding with existing ERP product, FBS and profit verification.
import fs from 'node:fs/promises';
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
 if(!category.eligible||category.holdout||engine.prohibitedCategoryMatch(product,rules)||blockedImportBrand(product,cfg.flow_f.blocked_source_brands)||feedbackBlockForProduct(product,feedback).blocked)return {rejected:'protected_product'};
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
  if(d?.outcome!=='approved'||!row||review.brand_or_model_conflict||!((Number(row.dinov2_similarity)>=0.86&&source.comparebot.search_and_rank.query.size==='small')||(Number(row.dinov2_similarity)>=0.82&&review.verdict==='match'))||!Number.isFinite(Number(row.dinov2_similarity))||Number(row.dinov2_similarity)<0.82||Number(row.dinov2_similarity)>1||String(source.selected_offer_id)!==String(d.selected_offer_id)
   ||!/^\d+$/.test(String(source.selected_offer_id))
   ||source.selected_offer_url!==`https://detail.1688.com/offer/${source.selected_offer_id}.html`
   ||!(Number(source.selected_cost_cny)>0)||!Number.isFinite(Number(source.selected_cost_cny))
   ||Number(source.selected_cost_cny)!==Number(row.candidate.price_cny)
   ||source.selected_image_url!==row.candidate.image_url)throw Error('invalid compareBot source binding');
 }
 if(feedbackBlockForPair(product,source,feedback).blocked)return {rejected:'human_mismatch'};
 const useLocalProfit=await fs.access(path.join(root,'FlowEF-production/state/production/local-profit.enabled')).then(()=>true).catch(()=>false);
 stage('commission_and_category');
 const [commissions,categoryData]=await Promise.all([useLocalProfit?Promise.resolve(null):client.listCategoryCommissions(),client.getCategoryBySku(product.sku)]);
 if(engine.prohibitedLeafCategoryMatch(categoryData))return {rejected:'prohibited_leaf'};
 stage('fbs_verification');
 const fbs=await engine.observePureFbs(transport,product,'flowef-production-decision');
 if(!fbs.verified)return {rejected:'source_unverified'};
 stage('postal_profit');
 let profit;
 if(useLocalProfit){
  const {localProfit}=await load('FlowEF-production/bridges/local-profit.mjs');
  profit=await localProfit(root,{product,category_data:categoryData,source,rub_cny:rubCny,sell_cny:engine.productSalePriceCny(product,rubCny)});
  if(profit.rejected==='no_logistics_route')throw Error('No eligible logistics route for ChinaPost');
  if(profit.rejected)throw Error('Local official commission or cost inputs unavailable');
 }else profit=await engine.maoziProfit({client,commissions,rubCny,product,categoryData,purchasePrice:source.selected_cost_cny});
 if(profit.input.logistics!=='ChinaPost')throw Error('postal route required');
 return {source,profit,fbs,product,exchange_rate_evidence:exchange,observed_at:new Date().toISOString()};
}
try{const result=await main();stage('finished');if(input.action==='evaluate')await fs.appendFile(path.join(root,'FlowEF-production/state/production/request-diagnostics.jsonl'),JSON.stringify({at:new Date().toISOString(),action:input.action,sku:input.product?.sku,ok:true,rejected:result.rejected||null,stage_ms:stageDurations,requests:requestTrace})+'\n',{mode:0o600}).catch(()=>{});process.stdout.write(JSON.stringify({ok:true,result}));}
catch(error){await fs.appendFile(path.join(root,'FlowEF-production/state/production/request-diagnostics.jsonl'),JSON.stringify({at:new Date().toISOString(),action:input.action,sku:input.product?.sku,diagnostic:error.networkDiagnostic||{stage:diagnosticStage,recent_requests:requestTrace}})+'\n',{mode:0o600}).catch(()=>{});process.stdout.write(JSON.stringify({ok:false,error:{code:error.code||error.name||'Error',message:String(error.message).replace(/Bearer\s+\S+/gi,'Bearer [redacted]').slice(0,700),diagnostic:error.networkDiagnostic||{stage:diagnosticStage,recent_requests:requestTrace},status:error.status,unknown:error.writeOutcomeUnknown===true,retry_after_ms:error.retryAfterMs}}));process.exitCode=1;}
