// Dedicated synthetic PostgreSQL schema; no review/product records are touched.
import pg from 'pg';
import assert from 'node:assert/strict';
import {systemSchema} from '../lib/system-schema.mjs';
import {serveSystem} from '../lib/system.mjs';
const schema='flowhub_control_test_'+Date.now();
const admin=new pg.Client({connectionString:process.env.DATABASE_URL});await admin.connect();
let n=0,now=Date.now()/1000;
const env={SYSTEM_DEPLOYMENT_ID:'test',SYSTEM_PUBLIC_ORIGIN:'https://example.org',SYSTEM_AGENT_TOKEN:'secret'};
const check=(x)=>{n++;assert.ok(x);};
async function call(path,body,extra={}){
 const c=new pg.Client({connectionString:process.env.DATABASE_URL});await c.connect();
 try{
  await c.query('BEGIN');await c.query(`SET LOCAL search_path TO ${schema}`);
  const request=new Request('https://example.org/api/system/v1/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',origin:'https://example.org','x-system-request':'1',authorization:'Bearer secret',...extra},...(body?{body:JSON.stringify(body)}:{})});
  const response=await serveSystem(c,request,env,()=>now);const data=await response.json();await c.query(response.status>=400?'ROLLBACK':'COMMIT');return {httpStatus:response.status,...data};
 }finally{await c.end();}
}
const snap={deployment:'test',status:{state:'running',observed_at:now,state_version:'v',source_revision:'r',controls_enabled:true},activity:[],progress:{coverage:{plugin_pipeline:now,plugin_publications:now},upload_pending_after_batch:{}},entities:[]};
try{
 await admin.query(`CREATE SCHEMA ${schema}`);await admin.query('BEGIN');await admin.query(`SET LOCAL search_path TO ${schema}`);await systemSchema(admin);await admin.query('COMMIT');
 check((await call('status')).stale);
 check((await call('agent/exchange',snap,{authorization:'bad'})).httpStatus===401);
 const success={owner:'o',sku:'s',seller:'a',backend:'maozi_follow',verified:true,offer_id:'offer',started_at:now-600,first_verified_at:now-300};
 const entities=[{kind:'success',key:'success1',body:success},{kind:'success',key:'duplicate',body:success},{kind:'publication',key:'p1',body:success},{kind:'product',key:'p1',body:{owner:'o',sku:'s',seller:'a',state:'selling'}},{kind:'product',key:'p2',body:{owner:'o',sku:'s',seller:'b',state:'publishing'}},{kind:'publication',key:'p2',body:{...success,seller:'b',first_verified_at:null}},{kind:'event',key:'1',body:{id:1,module:'submit',sku:'s',owner:'o',finished:now-100,duration_seconds:2,error_class:'sqlite_lock'}}];
 check((await call('agent/exchange',{...snap,entities})).httpStatus===200);
 check((await call('agent/exchange',{...snap,entities})).httpStatus===200);
 const m=await call('metrics');check(m.successes===1);check(m.cohort.enrolled===2);check(m.cohort.verified===1);check(m.cohort.success_rate_including_pending===.5);check(m.complete);check(m.failures===null);check(m.steps.length===1);
 check((await call('products/s?seller_id=a')).items.filter(r=>r.kind==='product').length===1);
 const page=await call('tasks?limit=1');check(page.items.length===1);check(Boolean(page.next_cursor));check((await call('tasks?limit=1&cursor='+page.next_cursor)).items[0].body.seller==='b');
 check((await call('errors')).items[0].error_class==='sqlite_lock');
 const range=await call('throughput?from='+(now-7200)+'&to='+now);check(range.hours.length===2);check(range.range.from===now-7200);check((await call('throughput?from=bad&to='+now)).httpStatus===422);
 const command={action:'pause',idempotency_key:'one',expected_state_version:'v',target_revision:'r',expires_at:now+100};
 const parallel=await Promise.all([call('commands',command),call('commands',command)]);check(parallel.filter(r=>r.httpStatus===202).length===1);check(parallel.some(r=>r.replayed));
 const cmd=parallel[0].command;check((await call('commands',{...command,action:'stop'})).httpStatus===409);
 check((await call('commands',{...command,idempotency_key:'two'})).httpStatus===409);
 check((await call('agent/exchange',snap)).command.id===cmd.id);check((await call('agent/exchange',snap)).command.id===cmd.id);
 check((await call('agent/exchange',{...snap,receipt:{id:cmd.id,status:'applied'}})).command===null);
 check((await call('commands/'+cmd.id)).terminal);
 const next=await call('commands',{...command,idempotency_key:'two'});check(next.httpStatus===202);
 now+=101;snap.status.observed_at=now;check((await call('agent/exchange',snap)).command===null);
 check((await call('commands/'+next.command.id)).receipt.status==='expired');
 now+=100;check((await call('status')).status.state==='unknown'); // response field status is system status
 check((await call('commands',{...command,expires_at:now+60,idempotency_key:'three'})).error==='agent_state_stale');
 console.log(JSON.stringify({passed:true,assertions:n,isolated_schema:schema,production_record_mutations:0}));
}finally{
 // This schema contains only this test's synthetic records, created above.
 await admin.query(`DROP SCHEMA ${schema} CASCADE`);await admin.end();
}
