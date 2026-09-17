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

test('direct dossiers require complete fresh exact field evidence',async()=>{
 const {verifiedPluginPublication}=await import('../../ozon-runtime/lib/plugin-source-policy.mjs');
 const p=product();p.profit_evaluation_only=true;p.allow_unknown_publication=true;p.price_evidence={value:40,currency:'CNY',observed_at:100};
 p.plugin_detail={...p.plugin_detail,contract:'direct-field-dossier-v1',weight_g:20,dimensions_mm:[10,20,30],attributes:[{id:1}],field_observations:Object.fromEntries(['weight_g','dimensions_mm','attributes'].map(k=>[k,{sku:'1',source:'maozi-erp-draft',observed_at:100}]))};
 assert.equal(verifiedPluginPublication(p,101),true);
 for(const change of [x=>x.plugin_detail.attributes=[],x=>x.plugin_detail.field_observations.attributes.sku='9',x=>x.plugin_detail.field_observations.attributes.observed_at=-30000,x=>x.plugin_detail.field_observations.attributes.source='unverified',x=>x.allow_unknown_publication=false]){
  const x=structuredClone(p);change(x);assert.equal(Boolean(verifiedPluginPublication(x,101)),false);
 }
});

test('website native import keeps identity and restrictions while allowing platform-managed attributes',async()=>{
 const {verifiedPluginPublication}=await import('../../ozon-runtime/lib/plugin-source-policy.mjs');
 const p=product();p.allow_unknown_publication=true;
 Object.assign(p.plugin_detail,{observed_at:1,weight_g:20,dimensions_mm:[10,20,30],attributes:[]});
 p.website_listing_authorization={id:'user-order-12345',sku:'1',at:30000,same_product_confirmed:true};
 p.approved_listing_price={kind:'approved_listing_price',sku:'1',currency:'CNY',value:40,decided_at:30000};
 assert.equal(Boolean(verifiedPluginPublication(p,30001)),true);
 for(const change of [x=>x.plugin_detail.monthly_sales.blocked_by_seller=true,x=>x.website_listing_authorization.sku='9',x=>x.plugin_detail.dimensions_mm=[],x=>delete x.approved_listing_price]){
  const x=structuredClone(p);change(x);assert.equal(Boolean(verifiedPluginPublication(x,30001)),false);
 }
});

test('authorized publication reuses static facts but not expired prices or changed bindings',async()=>{
 const {verifiedPluginPublication}=await import('../../ozon-runtime/lib/plugin-source-policy.mjs');
 const now=100+15*3600,p=product();
 p.profit_evaluation_only=true;p.allow_unknown_publication=true;
 p.approved_listing_price={kind:'approved_listing_price',sku:'1',currency:'CNY',value:40,decided_at:now};
 p.plugin_detail={...p.plugin_detail,contract:'direct-field-dossier-v1',weight_g:20,dimensions_mm:[10,20,30],attributes:[{id:1}],field_observations:Object.fromEntries(['weight_g','dimensions_mm','attributes'].map(k=>[k,{sku:'1',source:'maozi-erp-draft',observed_at:100}]))};
 assert.equal(Boolean(verifiedPluginPublication(p,now)),true);
 assert.equal(p.plugin_detail.observed_at,100);
 for(const change of [x=>x.approved_listing_price.decided_at=100,x=>x.allow_unknown_publication=false,x=>x.source_relation.seller_id='99',x=>x.plugin_detail.field_observations.attributes.source='unverified',x=>x.plugin_detail.monthly_sales.blocked_by_seller=true,x=>x.plugin_detail.monthly_sales.sales_schema='FBO']){
  const x=structuredClone(p);change(x);assert.equal(Boolean(verifiedPluginPublication(x,now)),false);
 }
 p.approved_listing_price.decided_at=100+7*86400;
 assert.equal(Boolean(verifiedPluginPublication(p,100+7*86400)),false);
});
