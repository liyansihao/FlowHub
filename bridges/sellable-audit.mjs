// Narrow native ERP boundary for owner-authorized audit removals.
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const root=path.resolve(import.meta.dirname,'../..');
const {createGloballyPacedMaoziTransport}=await import(pathToFileURL(path.join(root,'ozon-runtime/lib/maozi-transport.mjs')));
let raw='';for await(const chunk of process.stdin)raw+=chunk;
try{
 const x=JSON.parse(raw),method=x.method||'GET';
 const reads=['/api.shop/lists','/api.order.ozon/lists','/api.product.online/lists','/api.product.online/get_stock','/api.product.import_logs/index'];
 const writes=['/api.product.online/batch_update_stock','/api.product.online/archive','/api.product.online/sync_shop'];
 if(!(method==='GET'&&reads.includes(x.path)||method==='POST'&&x.execute===true&&writes.includes(x.path)))throw Error('outside_audit_contract');
 const api=createGloballyPacedMaoziTransport({token:process.env.MAOZI_ACCESS_TOKEN,env:{...process.env,MAOZI_HTTP_BACKEND:'native'},allowWrites:x.execute===true,reserveFutureSlot:true,maxWaitMs:60000,requestTimeoutMs:30000});
 const r=await api(x.path,{method,query:x.query||{},body:x.body??null});
 if(r.status!==200||Number(r.json?.code)!==1)throw Object.assign(Error('ERP request failed'),{status:r.status,code:r.json?.code});
 process.stdout.write(JSON.stringify({ok:true,data:r.json.data}));
}catch(e){process.stdout.write(JSON.stringify({ok:false,error:{type:e.name,code:e.code||e.cause?.code,status:e.status,unknown:e.writeOutcomeUnknown===true,retry_ms:e.retryAfterMs}}));process.exitCode=1;}
