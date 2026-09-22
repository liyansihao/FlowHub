// Acquisition only. Persistent connection; original global pacing ledger is retained.
import path from 'node:path';
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';
const root=process.env.FLOWHUB_LEGACY_ROOT||'/Users/mac/Desktop/ozon';
const {createGloballyPacedMaoziTransport}=await import(pathToFileURL(path.join(root,'ozon-runtime/lib/maozi-transport.mjs')));
let timing;
const api=createGloballyPacedMaoziTransport({token:process.env.MAOZI_ACCESS_TOKEN,
 env:{...process.env,MAOZI_HTTP_BACKEND:'native'},allowWrites:true,apiIntervalMs:2500,
 // Operation runners reserve fairly in the original shared queue. The business
 // tick yields while this bounded runner waits; no separate budget or HTTP retry.
 reserveFutureSlot:true,maxWaitMs:60000,requestTimeoutMs:25000,
 fetchImpl:async(...args)=>{
  const start=performance.now();timing.rate_wait_ms=start-timing.start;timing.dispatched=true;
  try {const response=await fetch(...args);const text=response.text.bind(response);
   response.text=async()=>{try{return await text();}finally{timing.network_ms=performance.now()-start;}};
   return response;
  }catch(error){timing.network_ms=performance.now()-start;throw error;}
 }});
const allowed={'/api.product.favorite/lists':'GET','/api.product.favorite/toggle':'POST','/api.product.favorite/edit_import':'POST','/api.product.collect/detail':'GET','/api.product.collect/lists':'GET','/api.exchange_rate/index':'GET','/api.tool/get_category_by_sku':'GET','/api.chrome/sku3':'POST'};
for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
 let c;timing={start:performance.now(),rate_wait_ms:0,network_ms:0,dispatched:false};
 try {
  c=JSON.parse(line);
  if(allowed[c.path]!==c.method)throw Error('unsupported source operation');
  if(c.path.endsWith('/detail')&&c.query?.is_online!==0)throw Error('only source drafts allowed');
  if(c.path.endsWith('/toggle')&&(c.body?.status!==true||!/^\d+$/.test(String(c.body?.productInfo?.sku))))throw Error('invalid favorite intent');
  const r=await api(c.path,{method:c.method,query:c.query||{},body:c.body||null});
  if(r.status===401||r.status===403||r.json?.code!==1)throw Object.assign(Error('ERP_SOURCE_REJECTED'),{status:r.status,apiCode:r.json?.code});
  const {start,...metrics}=timing;
  process.stdout.write(JSON.stringify({id:c.id,ok:true,data:r.json.data,timing:{...metrics,total_ms:performance.now()-start}})+'\n');
 }catch(error){
  const {start,...metrics}=timing;
  process.stdout.write(JSON.stringify({id:c?.id,ok:false,error:error.code||error.cause?.code||error.name,
   timing:{...metrics,total_ms:performance.now()-start},diagnostic:{http_status:error.status,api_code:error.apiCode,operation:c?.path,
    not_sent:!timing.dispatched,write_outcome_unknown:c?.method==='POST'&&c?.path!=='/api.chrome/sku3'&&timing.dispatched,
    retry_after_ms:error.retryAfterMs}})+'\n');
 }
}
