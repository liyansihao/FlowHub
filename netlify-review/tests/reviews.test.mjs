import test from 'node:test';
import assert from 'node:assert/strict';
import {createHandler, readSeedFile} from '../functions/reviews.mjs';
process.env.REVIEW_SYNC_TOKEN='test-sync';
const row=(revision='b'.repeat(64))=>({sku:'123',seller:'456',pipeline_state:'needs_review',revision,can_approve:false,can_repair:true,can_reject:true,allowed_actions:['repair','reject']});
function fixture() {
  const values=new Map();
  const store={get:async key=>structuredClone(values.get(key)??null),setJSON:async(key,value)=>{values.set(key,structuredClone(value));},list:async({prefix})=>({blobs:[...values.keys()].filter(x=>x.startsWith(prefix)).map(key=>({key}))})};
  const handler=createHandler({store,readSeed:async()=>({owner:'owner',items:[row('a'.repeat(64))]})});
  const request=async(mode='review',body,token='test-sync')=>{
    const result=await handler(new Request(`https://review.invalid/api/reviews?mode=${mode}`,{method:body?'POST':'GET',headers:{'content-type':'application/json','x-sync-token':token},body:body?JSON.stringify(body):undefined}));
    return {...await result.json(),status:result.status};
  };
  const refresh=items=>request('refresh',{owner:'owner',items});
  const submit=(item=row(),action='repair',note='补全资料')=>request('review',{...item,action,note,reviewer:'human'});
  return {values,request,refresh,submit,handler};
}

test('POST uses live snapshot, rejects seed revision and vanished rows',async()=>{
  const f=fixture();await f.refresh([row()]);
  assert.equal((await f.submit(row('a'.repeat(64)))).status,409);
  assert.equal((await f.submit()).status,200);
  await f.refresh([]);assert.equal((await f.submit()).status,404);
});
test('seed is display only until a local refresh arrives',async()=>{
  const f=fixture();assert.deepEqual((await f.request()).items[0].allowed_actions,[]);
  assert.equal((await f.submit(row('a'.repeat(64)))).status,409);
});
test('repair eligibility, approval gates, notes and methods are enforced',async()=>{
  const f=fixture();await f.refresh([row()]);
  assert.equal((await f.submit(row(),'approve')).status,409);
  assert.equal((await f.submit(row(),'repair','  ')).status,422);
  await f.refresh([{...row(),can_repair:false,allowed_actions:['reject']}]);
  assert.equal((await f.submit()).status,409);
  assert.equal((await f.request('ack')).status,405);
  assert.equal((await f.request('sync',undefined,'bad')).status,401);
});
test('new revision remains visible and actionable after previous applied decision',async()=>{
  const f=fixture();await f.refresh([row()]);const d=(await f.submit()).decision;
  await f.request('ack',{id:d.id,status:'applied'});
  const next=row('c'.repeat(64));await f.refresh([next]);
  assert.equal((await f.request()).items[0].decision,null);
  assert.equal((await f.submit(next)).status,200);
  assert.equal((await f.request()).history.length,2);
});
test('refresh and acknowledgements cannot overwrite decisions',async()=>{
  const f=fixture();await f.refresh([row()]);const d=(await f.submit()).decision;
  await Promise.all([f.refresh([]),f.request('ack',{id:d.id,status:'applied'})]);
  assert.equal(f.values.get('snapshot').items.length,0);
  assert.equal((await f.request('sync')).items.length,0);
  assert.equal((await f.request()).history[0].status,'applied');
  await f.request('ack',{id:d.id,status:'error',error:'late retry'});
  assert.equal((await f.request()).history[0].status,'applied');
});
test('rejected local decisions expose errors and permit correction',async()=>{
  const f=fixture();await f.refresh([row()]);const d=(await f.submit()).decision;
  assert.equal((await f.submit()).status,409);
  await f.request('ack',{id:d.id,status:'rejected',error:'测算已过期'});
  assert.equal((await f.request()).items[0].decision.last_error,'测算已过期');
  assert.equal((await f.submit()).status,200);
});
test('tenant changes are rejected and decisions are tenant-bound',async()=>{
  const f=fixture();await f.refresh([row()]);
  assert.equal((await f.submit()).decision.owner,'owner');
  assert.equal((await f.request('refresh',{owner:'other',items:[]})).status,409);
});


test('seed resolves both source and flattened function bundle layouts',async()=>{
  const fs=await import('node:fs/promises');
  const {tmpdir}=await import('node:os');
  const {join}=await import('node:path');
  const {pathToFileURL}=await import('node:url');
  const dir=await fs.mkdtemp(join(tmpdir(),'flowhub-seed-'));
  try {
    await fs.mkdir(join(dir,'data'));
    await fs.writeFile(join(dir,'data/reviews.json'),JSON.stringify({items:[]}));
    assert.deepEqual(await readSeedFile(pathToFileURL(join(dir,'functions/reviews.mjs'))),{items:[]});
    assert.deepEqual(await readSeedFile(pathToFileURL(join(dir,'reviews.mjs'))),{items:[]});
  } finally {await fs.rm(dir,{recursive:true,force:true});}
});

test('local identity decisions appear in history without entering remote command queue',async()=>{
  const f=fixture();
  const event={id:'local-1',sku:'123',seller:'456',revision:'d'.repeat(64),action:'approve',reviewer:'Codex',note:'same product',status:'applied',created_at:'2026-09-15T00:00:00Z'};
  await f.request('refresh',{owner:'owner',items:[],local_history:[event]});
  assert.equal((await f.request()).history[0].id,'local-1');
  assert.equal((await f.request('sync')).items.length,0);
});

test('every product remains visible with explicit listing controls after an applied action',async()=>{
 const f=fixture();const product={...row(),pipeline_state:'selling',can_list:true,can_unlist:true,allowed_actions:['list','unlist']};
 await f.refresh([product]);const result=await f.submit(product,'unlist','下架');assert.equal(result.status,200);
 await f.request('ack',{id:result.decision.id,status:'applied'});
 assert.equal((await f.request()).items.length,1);
});

test('paged products bound payload, filter all records and omit history',async()=>{
 const f=fixture();await f.refresh(Array.from({length:100},(_,i)=>({...row(),sku:String(i),title:`产品 ${i}`,pipeline_state:i===99?'selling':'needs_review'})));
 const get=async query=>(await f.handler(new Request('https://review.invalid/api/reviews?'+query))).json();
 const first=await get('page=0&page_size=12');assert.equal(first.items.length,12);assert.equal(first.total,100);assert.deepEqual(first.history,[]);
 const last=await get('page=99&page_size=12');assert.equal(last.page,8);assert.equal(last.items.length,4);
 const match=await get('page=0&state=selling&q=99');assert.equal(match.filtered_total,1);assert.equal(match.items[0].sku,'99');
 const empty=await get('page=0&q=not-found');assert.equal(empty.filtered_total,0);assert.equal(empty.pages,1);
});
test('history is independently paginated and never enters product pages',async()=>{
 const f=fixture();await f.request('refresh',{owner:'owner',items:[row()],local_history:Array.from({length:30},(_,i)=>({id:`local-${i}`,sku:String(i),created_at:'2026-09-15T00:00:00Z',action:'list'}))});
 const data=await(await f.handler(new Request('https://review.invalid/api/reviews?view=history&page=1&page_size=12'))).json();
 assert.equal(data.items.length,0);assert.equal(data.history.length,12);assert.equal(data.filtered_total,30);
});

test('submitted lifecycle items leave queue across revision changes but remain in all products',async()=>{
 const f=fixture();const product={...row(),can_list:true,allowed_actions:['list']};await f.refresh([product]);
 const d=(await f.submit(product,'list','上架')).decision;
 const get=async view=>(await f.handler(new Request(`https://review.invalid/api/reviews?page=0&view=${view}`))).json();
 assert.equal((await get('queue')).items.length,0);
 assert.equal((await get('products')).items.length,1);
 await f.request('ack',{id:d.id,status:'applied'});
 await f.refresh([{...product,revision:'e'.repeat(64),listing_state:'blocked',listing_error:'店铺满额'}]);
 assert.equal((await get('queue')).items.length,0);
 assert.equal((await get('products')).items.length,1);
 assert.equal((await get('history')).history[0].listing_error,'店铺满额');
});
test('rejected submitted actions stay out of queue and repair alone does not remove an item',async()=>{
 const f=fixture();await f.refresh([row()]);let d=(await f.submit(row(),'reject','不同款')).decision;
 await f.request('ack',{id:d.id,status:'rejected',error:'版本变化'});
 const queue=async()=>(await f.handler(new Request('https://review.invalid/api/reviews?page=0&view=queue'))).json();
 assert.equal((await queue()).items.length,0);
 const repairOnly=fixture();await repairOnly.refresh([row()]);await repairOnly.submit(row(),'repair','重新识别');
 const res=await repairOnly.handler(new Request('https://review.invalid/api/reviews?page=0&view=queue'));assert.equal((await res.json()).items.length,1);
});

 test('review queue only admits needs_review items across refreshes and counts before pagination',async()=>{
 const f=fixture();
 const get=async query=>(await f.handler(new Request('https://review.invalid/api/reviews?page=0&view=queue&'+query))).json();
 await f.refresh(['needs_review','selling','rejected','publishing','needs_fields'].map((pipeline_state,i)=>({...row(),sku:String(i),pipeline_state})));
 let result=await get('');assert.deepEqual(result.items.map(x=>x.sku),['0']);assert.equal(result.queue_total,1);assert.equal(result.total,5);
 assert.equal((await get('q=missing')).queue_total,1);
 await f.refresh([{...row(),sku:'0',pipeline_state:'selling'},{...row(),sku:'5',pipeline_state:'needs_review'},{...row(),sku:'6',pipeline_state:'rejected'}]);
 result=await get('');assert.deepEqual(result.items.map(x=>x.sku),['5']);assert.equal(result.filtered_total,1);assert.equal(result.queue_total,1);
 });
