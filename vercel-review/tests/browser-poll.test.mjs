import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const source=fs.readFileSync(new URL('../../netlify-review/public/app.js',import.meta.url),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
 const elements=new Map(),requests=[],responses=[];let timers=0;
 const element=id=>{if(!elements.has(id))elements.set(id,{value:'',hidden:false,innerHTML:'',textContent:'',disabled:false,setAttribute(){},addEventListener(){},closest(){return null;}});return elements.get(id);};
 const doc={hidden:false,activeElement:null,querySelector:element,querySelectorAll:()=>[],addEventListener(){}};
 const response=(code,etag)=>({status:code,ok:code===200,headers:new Map([['etag',etag]]),json:async()=>({items:[],history:[],pages:1,page:0,total:10,queue_total:2,filtered_total:2})});
 const context=vm.createContext({document:doc,localStorage:{getItem(){return ''},setItem(){}},URL,URLSearchParams,AbortController,console,setTimeout(){return ++timers;},clearTimeout(){},fetch:async(url,options)=>{requests.push({url,options});return responses.shift()??response(200,'"v1"');}});
 vm.runInContext(source,context);return {context,doc,requests,responses,response,element};
}
test('unchanged page uses ETag without rerender; switching pages never reuses rendered-page ETag',async()=>{
 const f=setup();await tick();assert.equal(f.requests.length,1);
 f.element('#list').innerHTML='preserve current content';f.responses.push(f.response(304,'"v1"'));
 await vm.runInContext('load({quiet:true})',f.context);assert.equal(f.requests[1].options.headers['if-none-match'],'"v1"');assert.equal(f.element('#list').innerHTML,'preserve current content');assert.equal(vm.runInContext('quietDelay',f.context),60000);
 vm.runInContext('page=1',f.context);await vm.runInContext('load()',f.context);assert.equal(f.requests[2].options.headers['if-none-match'],undefined);
 f.doc.hidden=true;await vm.runInContext('load({quiet:true})',f.context);assert.equal(f.requests.length,3);
});
test('successful decision clears old validators so next load is complete',async()=>{
 const f=setup();await tick();f.responses.push({...f.response(200,''),json:async()=>({ok:true})});
 await vm.runInContext("api('POST',{action:'reject'})",f.context);assert.equal(vm.runInContext('displayedEtag',f.context),'');
});
test('different cards submit concurrently, same card cannot duplicate, refresh waits for outstanding requests',async()=>{
 const f=setup();await tick();
 const pending=[];
 f.context.fetch=(url,options)=>new Promise((resolve,reject)=>pending.push({options,resolve,reject}));
 function card(id){
  const textarea={value:'test note'},hint={textContent:''};
  const node={dataset:{id},removed:false,querySelector:s=>s==='textarea'?textarea:hint,querySelectorAll:()=>[button],remove(){this.removed=true;}};
  const button={dataset:{action:'approve'},disabled:false,textContent:'approve',closest:()=>node};
  return {node,button,hint};
 }
 const a=card('a::s'),b=card('b::s');f.context.a=a.button;f.context.b=b.button;
 vm.runInContext("snapshot.items=['a','b'].map(sku=>({sku,seller:'s',revision:'1',allowed_actions:['approve']}))",f.context);
 const first=vm.runInContext('decide(a)',f.context);
 const second=vm.runInContext('decide(b)',f.context);
 await vm.runInContext('decide(a)',f.context);
 assert.equal(pending.length,2);assert.equal(a.button.disabled,true);assert.equal(b.button.disabled,true);
 assert.deepEqual(pending.map(p=>JSON.parse(p.options.body).sku),['a','b']);
 pending[1].resolve({...f.response(200,''),json:async()=>({ok:true})});await second;
 assert.equal(b.node.removed,true);assert.equal(a.node.removed,false);
 await vm.runInContext('load()',f.context);assert.equal(pending.length,2);
 pending[0].reject(Object.assign(new Error('timeout'),{name:'AbortError'}));await first;
 assert.match(a.hint.textContent,/超时/);assert.equal(a.node.removed,false);
 await vm.runInContext('decide(a)',f.context);assert.equal(pending.length,2);
 assert.equal(vm.runInContext('submitting.size',f.context),0);
});
