import {timingSafeEqual} from 'node:crypto';
const equal=(a,b)=>typeof a==='string'&&Buffer.byteLength(a)===Buffer.byteLength(b)&&timingSafeEqual(Buffer.from(a),Buffer.from(b));
const json=(data,status=200,headers={})=>Response.json(data,{status,headers:{'cache-control':'no-store',...headers}});
export const rowKey=r=>JSON.stringify([r.kind,r.store_id,String(r.scheme||''),String(r.id)]);
export async function serveOperations(c,request,secret){
 const url=new URL(request.url),mode=url.searchParams.get('mode')||'view';
 const sync=equal(request.headers.get('x-sync-token'),secret);
 if(!['GET','POST'].includes(request.method))return json({error:'method_not_allowed'},405);
 if(request.method==='POST'&&!sync){
  if(request.headers.get('origin')!==url.origin||request.headers.get('x-ops-request')!=='1')return json({error:'请求来源校验失败'},403);
 }
 let input={};if(request.method==='POST'){try{input=await request.json();}catch{return json({error:'invalid_json'},422);}}
 const get=async key=>(await c.query('SELECT value FROM flowhub_review_blobs WHERE key=$1',[key])).rows[0]?.value;
 const put=async(key,value)=>c.query('INSERT INTO flowhub_review_blobs(key,value) VALUES($1,$2::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',[key,JSON.stringify(value)]);
 if(mode==='snapshot'&&request.method==='POST'){
  if(!sync)return json({error:'unauthorized'},401);
  if(!Array.isArray(input.items)||input.items.length>20000||!Array.isArray(input.stores)||!input.items.every(r=>['returns','inventory'].includes(r.kind)&&typeof r.id==='string'&&typeof r.store_id==='string'&&(r.kind!=='inventory'||r.present===0)))return json({error:'invalid_snapshot'},422);
  if(new Set(input.items.map(rowKey)).size!==input.items.length)return json({error:'duplicate_rows'},422);
  // Whitelist public operating fields; shop API keys and chat content never belong here.
  const fields=['kind','id','scheme','store_id','store_name','name','offer_id','sku','order','created_at','price','currency','state','state_label','closed','reason','reason_cn','reason_source','buyer_comment','order_cancel_reason','return_stage','return_stage_label','stage_evidence','detail_checked_at','detail_error','present','synced','stale'];
  const items=input.items.map(r=>Object.fromEntries(fields.filter(k=>k in r).map(k=>[k,r[k]])));
  const stores=input.stores.map(s=>({id:String(s.id),name:String(s.name),channels:s.channels}));
  const old=await get('operations:snapshot'),keys=new Set(items.map(rowKey));
  for(const r of old?.items||[]){
   if(r.kind!=='inventory'||keys.has(rowKey(r)))continue;
   const key='operations:handled:'+rowKey(r),mark=await get(key);
   if(mark?.handled){const value={...mark,handled:false,version:mark.version+1,at:new Date().toISOString(),reason:'零库存预警已解除'};await put(key,value);await put(`operations:audit:${rowKey(r)}:${value.version}`,value);}
  }
  await put('operations:snapshot',{items,stores,uploaded_at:new Date().toISOString()});
  return json({ok:true,total:items.length});
 }
 if(mode==='markers'&&request.method==='GET'&&sync){
  return json({markers:(await c.query("SELECT value FROM flowhub_review_blobs WHERE starts_with(key,'operations:handled:')")).rows.map(x=>x.value)});
 }
 const snapshot=await get('operations:snapshot')||{items:[],stores:[],uploaded_at:null};
 if(mode==='handle'&&request.method==='POST'){
  if(typeof input.key!=='string'||typeof input.handled!=='boolean')return json({error:'invalid_handling'},422);
  if(!snapshot.items.some(r=>rowKey(r)===input.key))return json({error:'记录已变化，请刷新列表'},409);
  const key='operations:handled:'+input.key,previous=await get(key),version=previous?.version||0;
  if(input.version!==version)return json({error:'处理状态已被更新，请刷新后再操作'},409);
  const value={key:input.key,handled:input.handled,at:new Date().toISOString(),version:version+1};
  await put(key,value);
  await put(`operations:audit:${input.key}:${value.version}`,value);
  return json({ok:true,...value});
 }
 if(mode==='view'&&request.method==='GET'){
  const markers=(await c.query("SELECT value FROM flowhub_review_blobs WHERE starts_with(key,'operations:handled:')")).rows.map(x=>x.value);
  const marks=new Map(markers.map(x=>[x.key,x]));
  return json({...snapshot,items:snapshot.items.map(r=>({...r,key:rowKey(r),handling:marks.get(rowKey(r))||{handled:false,version:0}})),markers});
 }
 return json({error:'unknown_operation'},400);
}
