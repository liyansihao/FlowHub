import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {ReviewStore} from '../src/store.mjs';
function fixture(){
 const db=new DatabaseSync(':memory:');let selects=0,fail=false;
 const storage={sql:{exec(query,...args){if(query.startsWith('SELECT'))selects++;if(fail&&query.startsWith('INSERT'))throw Error('simulated write failure');const stmt=db.prepare(query);const rows=query.startsWith('SELECT')?stmt.all(...args):(stmt.run(...args),[]);return Object.assign(rows,{toArray:()=>rows});}},transactionSync(fn){db.exec('BEGIN');try{const result=fn();db.exec('COMMIT');return result;}catch(e){db.exec('ROLLBACK');throw e;}}};
 return {storage,store:new ReviewStore(storage),reads:()=>selects,fail:value=>fail=value};
}
test('polling and snapshot updates reuse cached data; restart restores persisted decisions',async()=>{
 const f=fixture();const item={sku:'123',seller:'456'};
 await f.store.setJSON('snapshot',{owner:'owner',items:[item]});await f.store.setJSON('decisions/1',{status:'pending'});
 const initial=f.reads();
 for(let i=0;i<100;i++){assert.equal((await f.store.get('snapshot')).items.length,1);assert.equal((await f.store.list({prefix:'decisions/'})).blobs.length,1);assert.equal((await f.store.get('decisions/1')).status,'pending');assert.equal(await f.store.get('state'),null);}
 assert.equal(f.reads(),initial);
 await f.store.setJSON('decisions/1',{status:'applied'});await f.store.setJSON('snapshot',{owner:'owner',items:[]});
 const restarted=new ReviewStore(f.storage);assert.equal((await restarted.get('decisions/1')).status,'applied');assert.deepEqual((await restarted.get('snapshot')).items,[]);
});
test('rolled-back transaction invalidates memory before the next read',async()=>{
 const f=fixture();await f.store.setJSON('decisions/1',{status:'pending'});
 assert.throws(()=>f.store.transaction(()=>{f.store.write('decisions/1',JSON.stringify({status:'applied'}));f.fail(true);f.store.write('decisions/2','{}');}));f.fail(false);
 assert.equal((await f.store.get('decisions/1')).status,'pending');assert.equal(await f.store.get('decisions/2'),null);
});
