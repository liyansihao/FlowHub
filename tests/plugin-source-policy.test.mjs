import test from 'node:test';
import assert from 'node:assert/strict';
import {verifiedPluginSource,verifiedPluginEvaluation} from '../../ozon-runtime/lib/plugin-source-policy.mjs';
const product=()=>({sku:'1',seller_id:'2',source_relation:{seller_id:'2'},expansion_source:{contract:'flowhub-same-seller-v1',seed_bindings:[{sku:'3'}]},plugin_detail:{contract:'maozi-plugin-sku-detail-v1',sku:'1',observed_at:100,monthly_sales:{observed_at:100,sales_schema:'FBS',blocked_by_seller:false}}});
test('verified explicit source may enter evaluation independently of ranking priority',()=>assert.equal(verifiedPluginSource(product(),101),true));
test('stale, other identity, mixed shipping and unknown followability do not qualify',()=>{
 for(const change of [p=>p.plugin_detail.sku='9',p=>p.source_relation.seller_id='9',p=>p.plugin_detail.observed_at=-30000,p=>p.plugin_detail.monthly_sales.sales_schema='FBO',p=>p.plugin_detail.monthly_sales.blocked_by_seller=null]){
  const p=product();change(p);assert.equal(Boolean(verifiedPluginSource(p,101)),false);
 }
});
test('priced evaluation with unknown shipping remains ineligible for publication',()=>{
 const p=product();delete p.plugin_detail.monthly_sales;
 p.profit_evaluation_only=true;p.price_evidence={value:49.71,currency:'CNY',observed_at:100};
 assert.equal(Boolean(verifiedPluginEvaluation(p,101)),true);
 assert.equal(Boolean(verifiedPluginSource(p,101)),false);
 for(const quote of [{value:NaN,currency:'CNY',observed_at:100},{value:10,currency:'USD',observed_at:100},{value:10,currency:'RUB',observed_at:-30000}]){
  p.price_evidence=quote;assert.equal(Boolean(verifiedPluginEvaluation(p,101)),false);
 }
});

test('explicit publication permission permits unknown facts but never affirmative restrictions or bad identity',async()=>{
 const {verifiedPluginPublication}=await import('../../ozon-runtime/lib/plugin-source-policy.mjs');
 const p=product();p.plugin_detail.monthly_sales={};p.profit_evaluation_only=true;p.price_evidence={value:40,currency:'CNY',observed_at:100};
 assert.equal(Boolean(verifiedPluginPublication(p,101)),false);
 p.allow_unknown_publication=true;
 assert.equal(Boolean(verifiedPluginPublication(p,101)),true);
 for(const change of [x=>x.plugin_detail.monthly_sales.sales_schema='FBO',x=>x.plugin_detail.monthly_sales.blocked_by_seller=true,x=>x.plugin_detail.sku='9']){
  const x=structuredClone(p);change(x);assert.equal(Boolean(verifiedPluginPublication(x,101)),false);
 }
});

test('asking-price intent permits old reference price without changing the observation',async()=>{
 const {verifiedPluginPublication}=await import('../../ozon-runtime/lib/plugin-source-policy.mjs');
 const p=product();p.plugin_detail.monthly_sales={};p.profit_evaluation_only=true;p.allow_unknown_publication=true;
 p.price_evidence={value:40,currency:'CNY',observed_at:-30000};
 p.approved_listing_price={kind:'approved_listing_price',sku:'1',currency:'CNY',value:40,decided_at:100};
 assert.equal(Boolean(verifiedPluginPublication(p,101)),true);
 assert.equal(p.price_evidence.observed_at,-30000);
 p.approved_listing_price.sku='9';assert.equal(Boolean(verifiedPluginPublication(p,101)),false);
});
