import test from 'node:test';
import assert from 'node:assert/strict';
import {escapeHTML,number,isStale,runningState,identities,hour} from '../public/operations/system-model.mjs';
test('unknown counts never become zero; stale or missing heartbeat never shows running',()=>{
 assert.equal(number(null),'—');assert.equal(number(undefined),'—');assert.equal(number(0),'0');
 assert.equal(runningState({observed_at:100,status:{state:'running'}},180),'running');
 assert.equal(runningState({observed_at:100,status:{state:'running'}},191),'unknown');
 assert.equal(runningState({stale:true,observed_at:100,status:{state:'running'}},101),'unknown');
 assert.equal(isStale({status:{state:'running'}},100),true);
});
test('product results group by owner and seller without merging identities',()=>{
 const groups=identities([{kind:'product',body:{owner:'a',sku:'1',seller:'s1',state:'selling'}},{kind:'publication',body:{owner:'a',sku:'1',seller:'s1',verified:true}},{kind:'product',body:{owner:'a',sku:'1',seller:'s2',state:'quarantined'}},{kind:'product',body:{owner:'b',sku:'1',seller:'s1',state:'publishing'}}]);
 assert.equal(groups.length,3);assert.equal(groups[0].publication.verified,true);assert.equal(groups[1].publication,undefined);assert.equal(groups[2].owner,'b');
});
test('rendered text escapes markup and hours always use Shanghai timezone',()=>{
 assert.equal(escapeHTML('<img src=x onerror="x">&\''),'&lt;img src=x onerror=&quot;x&quot;&gt;&amp;&#39;');
 assert.equal(hour(Date.parse('2026-09-23T00:00:00Z')/1000),'08');
});

import vm from 'node:vm';
import fs from 'node:fs';
import * as model from '../public/operations/system-model.mjs';
const source=fs.readFileSync(new URL('../public/operations/system.js',import.meta.url),'utf8').replace(/^import .*\n/,'');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
 const elements=new Map(),requests=[];
 function element(id){if(!elements.has(id))elements.set(id,{value:id==='sys-task-state'?'needs_review':'',innerHTML:'',textContent:'',hidden:false,disabled:false,open:false,events:{},addEventListener(k,f){this.events[k]=f;},showModal(){this.open=true;},close(){this.open=false;this.events.close?.();}});return elements.get(id);}
 const now=Date.now()/1000;
 const payload={observed_at:now,status:{state:'running',pending_task_count:8},items:[],hours:[],cohort:{enrolled:2,verified:1,not_verified:1,success_rate_including_pending:.5},successes:3,last_60_minutes:2,failures:null,steps:[],logs:[]};
 const context=vm.createContext({...model,esc:model.escapeHTML,num:model.number,document:{hidden:false,getElementById:element,addEventListener(){}},Date,URLSearchParams,AbortController,AbortSignal,console,setInterval(){},setTimeout,clearTimeout,fetch:async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>payload};}});
 vm.runInContext(source,context);return {context,element,requests,payload};
}
test('dashboard reads independently; partial failure preserves data and never sends a command',async()=>{
 const f=setup();await tick();await tick();
 assert.equal(f.element('sys-success').textContent,'3');assert.equal(f.element('sys-failures').textContent,'待核实');
 assert.equal(f.requests.length,5);assert.ok(f.requests.every(r=>!r.opts.method&&!r.url.includes('commands')));
 f.context.fetch=async url=>{if(url.endsWith('/status')||url.endsWith('/metrics'))throw new Error('offline');return {ok:true,json:async()=>f.payload};};
 await vm.runInContext('refresh()',f.context);
 assert.match(f.element('sys-status').innerHTML,/状态待确认/);assert.equal(f.element('sys-success').textContent,'3');
 assert.equal(f.element('sys-metrics-error').hidden,false);assert.equal(f.element('sys-activity-error').hidden,true);
});
test('task filter race cannot overwrite newer result; closed product cannot receive delayed result',async()=>{
 const f=setup();await tick();await tick();const pending=[];
 f.context.fetch=(url,opts)=>new Promise(resolve=>pending.push({url,opts,resolve}));
 const first=vm.runInContext('loadTasks()',f.context);f.element('sys-task-state').value='publishing';const second=vm.runInContext('loadTasks()',f.context);
 pending[1].resolve({ok:true,json:async()=>({...f.payload,items:[{body:{sku:'new',seller:'s',state:'publishing'}}]})});await second;
 pending[0].resolve({ok:true,json:async()=>({...f.payload,items:[{body:{sku:'old'}}]})});await first;
 assert.match(f.element('sys-tasks').innerHTML,/>new</);assert.doesNotMatch(f.element('sys-tasks').innerHTML,/>old</);
 const lookup=vm.runInContext("openProduct('123')",f.context);f.element('sys-product-dialog').close();
 pending[2].resolve({ok:true,json:async()=>({...f.payload,items:[{kind:'product',body:{sku:'123',state:'selling',seller:'s'}}]})});await lookup;
 assert.doesNotMatch(f.element('sys-product-body').innerHTML,/在售/);assert.equal(f.element('sys-product-dialog').open,false);
});

test('SQLite verification flags accept boolean and integer, without treating unknown as success',()=>{assert.equal(model.verifiedFlag(1),true);assert.equal(model.verifiedFlag(true),true);for(const value of [null,0,false,'1'])assert.equal(model.verifiedFlag(value),false);});
