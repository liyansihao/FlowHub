import test from 'node:test';
import assert from 'node:assert/strict';
import {collect,boundUrl} from '../bridges/storefront-auto.mjs';
function harness({read,pages=3,externalPause=false,slow=false}={}){
 let clock=0,task={state:'ready',page:1,next_url:'p1'},saved=[],events=[],attempts=0;
 const rpc=(action,arg={})=>{
  if(action==='status')return {...task};
  if(action==='pause'){task.state='paused';return {...task};}
  if(action==='failure'){task.state='blocked';task.error=arg.reason;return task;}
  if(action==='validate'){
   if(arg.html==='wrong')throw Error('cursor_mismatch');
   return {rows:2,explicit_end:false};
  }
  if(action==='ingest'){
   assert.equal(arg.url,task.next_url);
   saved.push(arg.html);task.page++;task.next_url='p'+task.page;
   if(task.page>pages)task.state='done';
   return {state:'committed',rows:2,added:2};
  }
 };
 return {options:{rpc,
  read:async(url)=>{attempts++;if(slow)clock+=11000;if(externalPause)task.state='paused';return read?read(url,attempts):{status:200,html:url};},
  save:async()=>'/fixture.html',event:async e=>events.push(e),seconds:10,interval:1000,
  now:()=>clock,delay:async ms=>{clock+=ms;}},
  state:()=>({task,saved,events,attempts})};
}
test('walks actual next cursor to explicit done without human interventions',async()=>{
 const h=harness();const r=await collect(h.options);
 assert.equal(r.committed_pages,3);assert.equal(r.stop_reason,'done');assert.equal(r.human_interventions,0);
 assert.deepEqual(h.state().saved,['p1','p2','p3']);
});
test('old page is retried at same checkpoint without duplicate commit',async()=>{
 const urls=[];const h=harness({pages:1,read:(url,n)=>{urls.push(url);return {status:200,html:n===1?'wrong':url};}});
 const r=await collect(h.options);assert.equal(r.retries,1);assert.deepEqual(urls,['p1','p1']);assert.equal(r.added_skus,2);
});
test('403 stops immediately; no repeated challenge requests',async()=>{
 const h=harness({read:()=>({status:403,html:'captcha'})});const r=await collect(h.options);
 assert.equal(r.stop_reason,'blocked');assert.equal(h.state().attempts,1);assert.equal(r.committed_pages,0);
});
test('external pause while page is loading prevents commit',async()=>{
 const h=harness({externalPause:true});const r=await collect(h.options);
 assert.equal(r.committed_pages,0);assert.equal(r.task.page,1);assert.equal(r.task.state,'paused');
});
test('deadline during request does not commit or move cursor',async()=>{
 const h=harness({slow:true});const r=await collect(h.options);
 assert.equal(r.committed_pages,0);assert.equal(r.task.page,1);assert.equal(r.task.state,'paused');
});
test('repeated transient failures have bounded retries and preserve cursor',async()=>{
 const h=harness({read:()=>({status:200,html:'wrong'})});h.options.seconds=30;
 const r=await collect(h.options);assert.equal(r.task.page,1);assert.equal(r.task.state,'blocked');assert.equal(h.state().attempts,3);
});
test('navigation boundaries reject offsite or another seller',()=>{
 assert.throws(()=>boundUrl('https://www.ozon.ru/seller/13/products/','12'),/identity/);
 assert.throws(()=>boundUrl('https://example.com/seller/12/products/','12'),/identity/);
 assert.equal(boundUrl('https://www.ozon.ru/seller/name-12/products/?page=2','12'),'https://www.ozon.ru/seller/name-12/products/?page=2');
});
test('local IPC timeout retries the same page automatically',async()=>{
 const h=harness({pages:1}), original=h.options.rpc;let failed=false;
 h.options.rpc=(action,arg)=>{
  if(action==='validate'&&!failed){failed=true;throw Error('local_rpc_timeout:validate:ETIMEDOUT');}
  if(action==='recover')return null;
  return original(action,arg);
 };
 const r=await collect(h.options);
 assert.equal(r.retries,1);assert.equal(r.committed_pages,1);assert.equal(r.task.state,'done');
});
test('lost IPC result after commit is reconciled without re-reading',async()=>{
 const h=harness({pages:1}),original=h.options.rpc;let recovered=null;
 h.options.rpc=(action,arg)=>{
  if(action==='ingest'){recovered=original(action,arg);throw Error('local_rpc_timeout:ingest:ETIMEDOUT');}
  if(action==='recover')return recovered;
  return original(action,arg);
 };
 const r=await collect(h.options);
 assert.equal(r.committed_pages,1);assert.equal(r.added_skus,2);assert.equal(h.state().attempts,1);
 assert.equal(h.state().events[0].type,'commit_recovered');
});
test('request rejected at deadline pauses rather than falsely blocking',async()=>{
 const h=harness({slow:true,read:()=>{throw Error('navigation_timeout');}});
 const r=await collect(h.options);
 assert.equal(r.stop_reason,'budget');assert.equal(r.task.state,'paused');assert.equal(r.task.page,1);
});
