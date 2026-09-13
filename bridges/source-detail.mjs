// Dedicated acquisition transport. Cannot publish, alter stock or update an online card.
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const root=process.env.FLOWHUB_LEGACY_ROOT||'/Users/mac/Desktop/ozon';
const {createGloballyPacedMaoziTransport}=await import(pathToFileURL(path.join(root,'ozon-runtime/lib/maozi-transport.mjs')));
let input='';for await(const chunk of process.stdin) input+=chunk;
try {
 const c=JSON.parse(input);
 const allowed={'/api.product.favorite/lists':'GET','/api.product.favorite/toggle':'POST','/api.product.favorite/edit_import':'POST','/api.product.collect/detail':'GET'};
 if(allowed[c.path]!==c.method)throw Error('unsupported source acquisition operation');
 if(c.path.endsWith('/detail')&&c.query?.is_online!==0)throw Error('only source drafts allowed');
 if(c.path.endsWith('/toggle')&&c.body?.status!==true)throw Error('only favorite creation allowed');
 const api=createGloballyPacedMaoziTransport({token:process.env.MAOZI_ACCESS_TOKEN,env:{...process.env,MAOZI_HTTP_BACKEND:'native'},allowWrites:true,apiIntervalMs:2500,reserveFutureSlot:true,maxWaitMs:60000,requestTimeoutMs:25000});
 const r=await api(c.path,{method:c.method,query:c.query||{},body:c.body||null});
 if(r.json?.code!==1)throw Object.assign(Error('ERP source acquisition rejected'), {code:'ERP_SOURCE_REJECTED',status:r.status});
 process.stdout.write(JSON.stringify({ok:true,data:r.json.data}));
}catch(error){
 const code=error.code||error.cause?.code||'SOURCE_REQUEST_FAILED';
 process.stdout.write(JSON.stringify({ok:false,error:code,diagnostic:{
  exception:error.name,http_status:error.status||null,cause_code:error.cause?.code||null,
  retry_after_ms:error.retryAfterMs||null,write_outcome_unknown:error.writeOutcomeUnknown===true
 }}));process.exitCode=1;
}
