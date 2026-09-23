// Persistent native ERP requests; existing global pacing and write contract stay in force.
import path from 'node:path';
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';
import {channel} from 'node:diagnostics_channel';
import {connectionErrorDetails} from './connection-error.mjs';
const root=process.env.FLOWEF_LEGACY_ROOT;
const {createGloballyPacedMaoziTransport}=await import(pathToFileURL(path.join(root,'ozon-runtime/lib/maozi-transport.mjs')));
let timing=null;
const connects=new Map();
channel('undici:client:beforeConnect').subscribe(({connectParams})=>connects.set(JSON.stringify(connectParams),performance.now()));
for(const name of ['connected','connectError'])channel('undici:client:'+name).subscribe(({connectParams,error})=>{
 const start=connects.get(JSON.stringify(connectParams));connects.delete(JSON.stringify(connectParams));
 if(timing&&error) timing.connection_error=connectionErrorDetails(error);
 if(timing&&start!==undefined){timing.connect_ms+=performance.now()-start;timing.connections++;}
});
const api=createGloballyPacedMaoziTransport({token:process.env.MAOZI_ACCESS_TOKEN,
 env:{...process.env,MAOZI_HTTP_BACKEND:'native'},allowWrites:true,apiIntervalMs:2500,
 reserveFutureSlot:true,maxWaitMs:60000,requestTimeoutMs:30000,
 fetchImpl:async(...args)=>{
  const start=performance.now();timing.pacing_wait_ms+=start-timing.last;
  try {
   const response=await fetch(...args);const text=response.text.bind(response);
   response.text=async()=>{try{return await text();}finally{timing.network_ms+=performance.now()-start;timing.last=performance.now();}};
   return response;
  }catch(error){timing.network_ms+=performance.now()-start;timing.last=performance.now();throw error;}
 }});
const reads=new Set(['/api.shop/lists','/api.product.favorite/lists','/api.product.import_logs/index','/api.product.online/lists','/api.product.online/get_stock']);
const writes=new Set(['/api.product.favorite/toggle','/api.selection.follow/import','/api.product.online/batch_update_stock','/api.product.online/sync_shop']);
for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
 let input;const began=performance.now();timing={last:began,pacing_wait_ms:0,network_ms:0,connect_ms:0,connections:0};
 try {
  input=JSON.parse(line);const method=input.method||'GET';
  if(input.action!=='request'||!(method==='GET'&&reads.has(input.path)||method==='POST'&&(input.path==='/api.chrome/sku3'||input.execute===true&&writes.has(input.path))))throw Error('request outside publication contract');
  const result=await api(input.path,{method,query:input.query||{},body:input.body??null});
  const {last,...metrics}=timing;
  process.stdout.write(JSON.stringify({id:input.id,ok:true,result,timing:{...metrics,total_ms:performance.now()-began}})+'\n');
 }catch(error){
  const {last,...metrics}=timing;
  process.stdout.write(JSON.stringify({id:input?.id,ok:false,timing:{...metrics,total_ms:performance.now()-began},error:{
   code:error.code||error.cause?.code||error.name,message:String(error.message).replaceAll(process.env.MAOZI_ACCESS_TOKEN,'[redacted]').slice(0,400),
   status:error.status,unknown:error.writeOutcomeUnknown===true,retry_after_ms:error.retryAfterMs}})+'\n');
 }
}
