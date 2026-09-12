// Read-only sourcing through the repository's globally paced ERP transport.
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const root=process.env.FLOWHUB_LEGACY_ROOT || path.resolve(import.meta.dirname,'../..');
const load=p=>import(pathToFileURL(path.join(root,p)).href);
let input=''; for await (const chunk of process.stdin) input+=chunk;
try {
 const c=JSON.parse(input);
 if(c.path==='native_shop') {
  const {nativeShopPage}=await import('./native-shop.mjs');
  process.stdout.write(JSON.stringify(await nativeShopPage(c.query.seller_id,c.query.page)));
  process.exit(0);
 }
 const allowed=new Set(['/api.selection.top/lists','/api.selection.keyword/reverse','/api.product.online/lists','/api.order.ozon/lists','/api.chrome/sku3']);
 if(!allowed.has(c.path))throw Object.assign(Error('unsupported'),{code:'unsupported'});
 const {createGloballyPacedMaoziTransport}=await load('ozon-runtime/lib/maozi-transport.mjs');
 const api=createGloballyPacedMaoziTransport({token:process.env.MAOZI_ACCESS_TOKEN,env:{...process.env,MAOZI_HTTP_BACKEND:'native'},allowWrites:false,apiIntervalMs:2500,reserveFutureSlot:true,maxWaitMs:60000,requestTimeoutMs:20000});
 const live=['/api.chrome/sku3','/api.selection.keyword/reverse'].includes(c.path);
 const r=await api(c.path,{method:live?'POST':'GET',query:c.query,...(live?{body:c.query}:{})});
 if(r.status===401||r.status===403)throw Object.assign(Error('authentication'),{code:'authentication'});
 if(r.status!==200||Number(r.json?.code)!==1)throw Object.assign(Error('unavailable'),{code:r.status===429?'rate_limit':'network',status:r.status,providerCode:r.json?.code});
 process.stdout.write(JSON.stringify({ok:true,data:r.json.data}));
}catch(e){process.stdout.write(JSON.stringify({ok:false,error:['authentication','unsupported','rate_limit'].includes(e.code)?e.code:'network',diagnostic:{exception:e.name,code:e.code||null,cause_code:e.cause?.code||null,http_status:e.status||null,provider_code:e.providerCode??null}}));process.exitCode=1;}
