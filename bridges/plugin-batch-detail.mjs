import fs from 'node:fs/promises';
import path from 'node:path';
import {openCliCommand} from './storefront-opencli.mjs';
const [sku,seller,out]=process.argv.slice(2);
const refresh=process.argv.includes('--refresh');
if(!/^\d+$/.test(sku)||!/^\d+$/.test(seller)||!out)throw Error('invalid_identity');
await fs.mkdir(out,{recursive:true});
const file=path.join(out,sku+'.json');let saved={sku,seller};
try{saved=JSON.parse(await fs.readFile(file,'utf8'));}catch{}
const save=async()=>{await fs.writeFile(file+'.tmp',JSON.stringify(saved),{mode:0o600});await fs.rename(file+'.tmp',file);};
const session='flowhub-plugin-details';
let tab='55DAAFC55A274ACAC17D3D1FB8B30E32';
const call=async(endpoint,body)=>{
 const code=`(async()=>{if(location.origin!=='https://seller.ozon.ru')throw Error('wrong_origin');const company=document.cookie.split('; ').find(x=>x.startsWith('sc_company_id='))?.split('=').slice(1).join('=');if(!company)throw Error('company_unavailable');let body=${JSON.stringify(body)};if(body.company_id==='SESSION')body.company_id=decodeURIComponent(company);const r=await fetch(${JSON.stringify(endpoint)},{method:'POST',credentials:'include',headers:{'Content-Type':'application/json','x-o3-company-id':decodeURIComponent(company),'x-o3-language':'RU'},body:JSON.stringify(body),signal:AbortSignal.timeout(20000)});return {status:r.status,data:await r.json()};})()`;
 try{return await openCliCommand(session,['eval',code,'--tab',tab],30000);}
 catch(error){
  // Never repeat a template creation whose response may have been lost.
  if(endpoint.includes('create-bundle'))throw error;
  const opened=await openCliCommand(session,['open','https://seller.ozon.ru/app/analytics/what-to-sell']);
  tab=opened.page;if(!tab)throw Error('missing_tab');
  return await openCliCommand(session,['eval',code,'--tab',tab],30000);
 }
};
try{
 if(refresh||!saved.sales||Date.now()/1000-saved.sales.observed_at>21600){
 if(saved.sales)await fs.appendFile(file+'.sales-history.jsonl',JSON.stringify(saved.sales)+'\n',{mode:0o600});
 saved.sales={sku,observed_at:Date.now()/1000,result:await call('/api/site/seller-analytics/what_to_sell/data/v3',{limit:'50',offset:'0',filter:{stock:'any_stock',period:'monthly',categories:[],sku},sort:{key:'sum_gmv_desc'}})};await save();}
 const matches=(saved.sales.result.data?.items||[]).filter(x=>String(x.sku)===sku&&String(x.sellerId)===seller);
 const sale=matches[0];
 if(!saved.base||saved.base.status!==200||!(saved.base.data?.variants||[]).some(x=>(x.skus||[]).map(String).includes(sku))){saved.base=await call('/api/v1/search',{company_id:'SESSION',need_total:true,filter:{children_nodes:{children_nodes:[{input_leaf:{sku:{values:[sku]}}}],operator:'AND'}},pagination:{limit:'50'},is_copy_allowed:false});await save();}
 if(saved.base.status!==200)throw Error('base_http_'+saved.base.status);
 const variants=(saved.base.data.variants||[]).filter(x=>(x.skus||[]).map(String).includes(sku));
 if(variants.length!==1)throw Error('base_variant_identity_mismatch');
 const variant=variants[0].variant_id;
 if(sale?.variantId&&String(sale.variantId)!==String(variant))throw Error('sales_variant_identity_mismatch');
 if(!saved.variant){
 if(saved.bundle_dispatched_at)throw Error('bundle_response_unknown_no_repeat');
 saved.bundle_dispatched_at=Date.now()/1000;await save();
 const detail=await call('/api/site/seller-prototype/create-bundle-by-variant-id',{company_id:'SESSION',variant_id:variant,source:'SOURCE_UI_COPY_MERGED'});
 saved.variant={sku,observed_at:Date.now()/1000,result:{base:saved.base,variant,detail}};await save();
 }
 if(saved.variant.result.detail.status!==200)throw Error('detail_http_'+saved.variant.result.detail.status);
 saved.state='ready';delete saved.error;await save();console.log(JSON.stringify({state:'ready',file}));
}catch(e){saved.state='needs_data';saved.error=String(e.message);await save();console.log(JSON.stringify({state:saved.state,error:saved.error,file}));}
