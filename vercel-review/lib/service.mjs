import {createHash,randomUUID} from 'node:crypto';
import {Repository} from './repository.mjs';
const json=(data,status=200,headers={})=>Response.json(data,{status,headers:{'cache-control':'no-store',...headers}});
const actions=['approve','reject','repair','list','unlist'];
const itemOK=x=>x&&typeof x.sku==='string'&&typeof x.seller==='string'&&/^[a-f0-9]{64}$/.test(x.revision);
const historyOK=x=>itemOK(x)&&typeof x.id==='string'&&x.id.length<=150&&actions.includes(x.action)&&['pending','applied','rejected'].includes(x.status)&&typeof x.created_at==='string';
const unique=items=>new Set(items.map(x=>JSON.stringify([x.sku,x.seller]))).size===items.length;
const permitted=(x,a)=>Array.isArray(x.allowed_actions)&&x.allowed_actions.includes(a)&&(!['approve','repair','list','unlist'].includes(a)||x[`can_${a}`]===true);
export async function serve(client,request,token){
 const url=new URL(request.url),mode=url.searchParams.get('mode')||'review',method=request.method;
 const methods={review:['GET','POST'],sync:['GET'],ack:['POST'],refresh:['POST'],delta:['POST'],'import-history':['POST']};
 if(!methods[mode])return json({error:'未知操作'},400);
 if(!methods[mode].includes(method))return json({error:'method_not_allowed'},405);
 if(mode!=='review'&&(!token||request.headers.get('x-sync-token')!==token))return json({error:'unauthorized'},401);
 let input={};if(method==='POST'){try{input=await request.json();}catch{return json({error:'请求正文必须是 JSON'},422);}if(!input||typeof input!=='object'||Array.isArray(input))return json({error:'invalid_body'},422);}
 const repo=new Repository(client);let meta=await repo.meta();
 if(mode==='sync')return json({items:meta?await repo.pending(meta.owner):[],cursor:meta?.cursor??'',viewer_active:meta?.viewer_active??false,protocol:'delta-v1'});
 if(mode==='refresh'||mode==='delta'){
  const items=mode==='refresh'?input.items:input.upserts,removed=mode==='delta'?(input.removed??[]):[],history=input.local_history??input.history??[];
  if(typeof input.owner!=='string'||!input.owner||!Array.isArray(items)||items.length>10000||!items.every(itemOK)||!unique(items)||!Array.isArray(history)||history.length>5000||!history.every(historyOK)||!Array.isArray(removed)||removed.length>10000||removed.some(x=>!x||typeof x.sku!=='string'||typeof x.seller!=='string'))return json({error:'invalid_snapshot'},422);
  if(meta&&meta.owner!==input.owner)return json({error:'审核台已绑定其他租户'},409);
  if(mode==='delta'){
   if(typeof input.cursor!=='string'||input.cursor.length>128||typeof input.base_cursor!=='string')return json({error:'invalid_cursor'},422);
   if(meta?.cursor===input.cursor)return json({ok:true,cursor:meta.cursor,changed:0,replayed:true});
   if(!meta||meta.cursor!==input.base_cursor)return json({error:'sync_cursor_mismatch',cursor:meta?.cursor??''},409);
  }
  if(!meta){await client.query('INSERT INTO flowhub_review_meta(id,owner) VALUES(1,$1)',[input.owner]);meta=await repo.meta();}
  const changed=(mode==='refresh'?await repo.replaceProducts(input.owner,items):await repo.upsertProducts(input.owner,items)+await repo.removeProducts(input.owner,removed))+await repo.importHistory(input.owner,history);
  // Heartbeats and no-op uploads must not invalidate the browser's content version.
  await client.query('UPDATE flowhub_review_meta SET cursor=$1,generated_at=$2,version=version+$3 WHERE id=1',[input.cursor??'',input.generated_at??new Date().toISOString(),changed?1:0]);
  const count=(await client.query('SELECT count(*)::int n FROM flowhub_review_products WHERE owner=$1',[input.owner])).rows[0].n;
  return json({ok:true,total:count,cursor:input.cursor??'',changed});
 }
 if(mode==='import-history'){
  if(!meta||input.owner!==meta.owner||!Array.isArray(input.items)||input.items.length>1000||!input.items.every(historyOK))return json({error:'invalid_history'},422);
  const imported=await repo.importHistory(meta.owner,input.items);if(imported)await repo.bump();return json({ok:true,imported});
 }
 if(mode==='ack'){
  if(!['applied','rejected','error'].includes(input.status))return json({error:'invalid_status'},422);
  const item=await repo.decision(input.id);if(!item||item.owner!==meta?.owner)return json({error:'decision_not_found'},404);
  if(['applied','rejected'].includes(item.status))return json({ok:true,status:item.status});
  const updated={...item,status:input.status==='error'?'pending':input.status,last_error:String(input.error||'').slice(0,500),acked_at:new Date().toISOString()};
  await repo.ack(updated);await repo.bump();return json({ok:true,status:updated.status});
 }
 if(method==='GET'){
  if(!meta)return json({generated_at:null,total:0,queue_total:0,filtered_total:0,page:0,page_size:12,pages:1,items:[],history:[],version:'0'});
  // This bounded write records human presence only, not automated sync polling.
  const options={view:url.searchParams.get('view')||'queue',q:(url.searchParams.get('q')||'').trim().slice(0,500),state:url.searchParams.get('state')||'',size:Math.max(1,Math.min(48,parseInt(url.searchParams.get('page_size'),10)||12)),requestedPage:Math.max(0,parseInt(url.searchParams.get('page'),10)||0)};
  const etag='"'+createHash('sha256').update(JSON.stringify([meta.version,options])).digest('base64url')+'"';
  if(request.headers.get('if-none-match')===etag)return new Response(null,{status:304,headers:{etag,'cache-control':'private, no-cache'}});
  const stats=await repo.stats(meta.owner),page=options.view==='history'?await repo.history(meta.owner,options):await repo.products(meta.owner,options);
  return json({generated_at:meta.generated_at,...stats,...page,page_size:options.size,version:String(meta.version)},200,{etag});
 }
 if(!meta)return json({error:'等待本机同步最新审核快照，请稍后刷新'},409);
 const item=await repo.product(meta.owner,input.sku,input.seller);if(!item)return json({error:'商品已不在当前待审队列，请刷新'},404);
 if(!actions.includes(input.action))return json({error:'invalid_action'},422);
 if(input.revision!==item.revision)return json({error:'审核版本已变化，请刷新页面'},409);
 if(!permitted(item,input.action))return json({error:(input.action==='repair'?item.repair_block:item.approval_block)||'当前商品不允许此操作'},409);
 if(typeof input.note!=='string'||!input.note.trim()||input.note.length>1000)return json({error:'请填写1至1000字的审核理由'},422);
 if(await repo.duplicate(meta.owner,item))return json({error:'该版本已有审核决定，请等待同步或刷新'},409);
 const decision={id:randomUUID(),owner:meta.owner,sku:item.sku,seller:item.seller,revision:item.revision,action:input.action,note:input.note.trim(),reviewer:String(input.reviewer||'匿名审核人').trim().slice(0,80)||'匿名审核人',status:'pending',created_at:new Date().toISOString()};
 await repo.importHistory(meta.owner,[decision]);await repo.bump();await repo.touch();return json({ok:true,status:'pending',decision});
}
