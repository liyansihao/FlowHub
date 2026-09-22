import test from 'node:test';
import assert from 'node:assert/strict';
import {serveOperations,accessToken,rowKey} from '../lib/operations.mjs';
const secret='test-sync-secret',base='https://flowhub-review.vercel.app/api/operations';
function db(){const rows=new Map();return {rows,async query(sql,args=[]){if(sql.startsWith('INSERT')){rows.set(args[0],JSON.parse(args[1]));return {rows:[]};}if(sql.includes('starts_with'))return {rows:[...rows].filter(([k])=>k.startsWith('operations:handled:')).map(([,value])=>({value}))};return {rows:rows.has(args[0])?[{value:rows.get(args[0])}]:[]};}};}
const request=(mode,body,headers={'x-sync-token':secret})=>new Request(base+'?mode='+mode,{method:body?'POST':'GET',headers:{'content-type':'application/json',...headers},...(body?{body:JSON.stringify(body)}:{})});
const row={kind:'inventory',id:'1',store_id:'a',present:0,name:'商品',api_key:'must-not-upload'};
test('private operations requires auth; session is same-origin and secure; sync permission is separate',async()=>{
 const c=db();assert.equal((await serveOperations(c,request('view',null,{}),secret)).status,401);
 assert.equal((await serveOperations(c,request('session',{token:accessToken(secret)},{origin:'https://evil.invalid','x-ops-request':'1'}),secret)).status,403);
 const r=await serveOperations(c,request('session',{token:accessToken(secret)},{origin:new URL(base).origin,'x-ops-request':'1'}),secret);
 assert.equal(r.status,200);assert.match(r.headers.get('set-cookie'),/HttpOnly; Secure; SameSite=Strict/);
 const headers={cookie:'ops_access='+accessToken(secret),origin:new URL(base).origin,'x-ops-request':'1'};
 assert.equal((await serveOperations(c,request('view',null,headers),secret)).status,200);
 assert.equal((await serveOperations(c,request('snapshot',{items:[],stores:[]},headers),secret)).status,401);
});
test('handling persists across snapshots, uses versions, keeps shops separate and reopens new stock episode',async()=>{
 const c=db(),snapshot={items:[row,{...row,store_id:'b'}],stores:[{id:'a',name:'A',api_key:'secret'}]};
 assert.equal((await serveOperations(c,request('snapshot',snapshot),secret)).status,200);
 assert.equal(c.rows.get('operations:snapshot').items[0].api_key,undefined);
 const action={key:rowKey(row),handled:true,version:0};
 assert.equal((await serveOperations(c,request('handle',action),secret)).status,200);
 assert.equal((await serveOperations(c,request('handle',action),secret)).status,409);
 await serveOperations(c,request('snapshot',snapshot),secret);
 let d=await (await serveOperations(c,request('view'),secret)).json();
 assert.equal(d.items[0].handling.handled,true);assert.equal(d.items[1].handling.handled,false);
 await serveOperations(c,request('handle',{...action,handled:false,version:1}),secret);
 await serveOperations(c,request('handle',{...action,version:2}),secret);
 await serveOperations(c,request('snapshot',{...snapshot,items:[]}),secret);
 await serveOperations(c,request('snapshot',snapshot),secret);
 d=await (await serveOperations(c,request('view'),secret)).json();assert.equal(d.items[0].handling.handled,false);assert.equal(d.items[0].handling.version,4);
 assert.equal((await serveOperations(c,request('handle',{...action,key:'unknown'}),secret)).status,409);
});
