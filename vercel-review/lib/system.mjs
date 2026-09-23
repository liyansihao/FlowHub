import {randomUUID,timingSafeEqual} from 'node:crypto';
export const TERMINAL=new Set(['applied','rejected','expired','needs_attention']);
const json=(body,status=200)=>Response.json(body,{status,headers:{'Cache-Control':'no-store'}});
const allowedKinds=new Set(['product','publication','success','event','log','failure']);
const fail=(reason,status=422)=>Object.assign(new Error(reason),{status});
function object(x){return x&&typeof x==='object'&&!Array.isArray(x);}
function text(x,max=128){return typeof x==='string'&&x.length>0&&x.length<=max;}
export function validToken(actual,expected){
 if(!expected||!actual)return false;
 const a=Buffer.from(actual),b=Buffer.from('Bearer '+expected);
 return a.length===b.length&&timingSafeEqual(a,b);
}
export function authorize(request,env){
 const path=new URL(request.url).pathname;
 if(path.endsWith('/agent/exchange')){
  if(!validToken(request.headers.get('authorization'),env.SYSTEM_AGENT_TOKEN))throw fail('invalid_device_token',401);
 }else if(request.method==='POST'){
  if(request.headers.get('origin')!==env.SYSTEM_PUBLIC_ORIGIN||request.headers.get('x-system-request')!=='1')throw fail('same_origin_request_required',403);
 }
}
export function validateCommand(body,snapshot,now){
 if(!object(body)||Object.keys(body).some(k=>!['action','idempotency_key','expected_state_version','target_revision','expires_at'].includes(k)))throw fail('invalid_command_fields');
 if(!['start','resume','pause','stop'].includes(body.action))throw fail('invalid_action');
 if(!text(body.idempotency_key)||!text(body.expected_state_version)||!text(body.target_revision))throw fail('missing_command_identity');
 if(!Number.isFinite(body.expires_at)||body.expires_at<=now||body.expires_at>now+1800)throw fail('invalid_expiry');
 if(!snapshot||now-snapshot.status?.observed_at>90||!Number.isFinite(snapshot.status?.observed_at))throw fail('agent_state_stale',409);
 if(!snapshot.status.controls_enabled)throw fail('controls_disabled',409);
 if(body.expected_state_version!==snapshot.status.state_version||body.target_revision!==snapshot.status.source_revision)throw fail('state_or_version_conflict',409);
 return body;
}
export function dayRange(date,now=Date.now()/1000){
 date??=new Date(now*1000+8*3600000).toISOString().slice(0,10);
 if(!/^\d{4}-\d\d-\d\d$/.test(date))throw fail('invalid_date');
 const start=Date.parse(date+'T00:00:00+08:00')/1000;
 if(!Number.isFinite(start)||new Date(start*1000+8*3600000).toISOString().slice(0,10)!==date)throw fail('invalid_date');
 return {date,start,end:start+86400};
}
export async function serveSystem(c,request,env,clock=()=>Date.now()/1000){
 try{
  authorize(request,env);
  const url=new URL(request.url),path=url.pathname.replace(/^\/api\/system\/v1\/?/,'');
  const deployment=env.SYSTEM_DEPLOYMENT_ID,now=clock();
  const q=(sql,args=[])=>c.query(sql,args);
  if(!deployment)throw fail('deployment_not_configured',503);
  if(request.method==='POST'&&path==='agent/exchange'){
   const b=await request.json();
   if(!object(b)||b.deployment!==deployment||!object(b.status)||!Array.isArray(b.entities)||b.entities.length>1000||!Array.isArray(b.activity)||b.activity.length>200)throw fail('invalid_exchange');
   if(!Number.isFinite(b.status.observed_at)||Math.abs(now-b.status.observed_at)>120)throw fail('invalid_observation_time');
   for(const e of b.entities)if(!object(e)||!allowedKinds.has(e.kind)||!text(e.key,200)||!object(e.body)||JSON.stringify(e.body).length>16000)throw fail('invalid_entity');
   await q('SELECT pg_advisory_xact_lock(72841923)');
   await q(`INSERT INTO flowhub_system_deployments VALUES($1,$2,now()) ON CONFLICT(id)
     DO UPDATE SET snapshot=excluded.snapshot,received_at=now()`,[deployment,JSON.stringify({status:b.status,activity:b.activity,progress:b.progress??{}})]);
   if(b.entities.length)await q(`INSERT INTO flowhub_system_entities(deployment,kind,key,body)
      SELECT $1,e.kind,e.key,e.body FROM jsonb_to_recordset($2::jsonb) AS e(kind text,key text,body jsonb)
      ON CONFLICT(deployment,kind,key) DO UPDATE SET body=excluded.body,received_at=now()`,[deployment,JSON.stringify(b.entities)]);
   if(b.receipt){
    if(!text(b.receipt.id)||!['running','draining',...TERMINAL].includes(b.receipt.status))throw fail('invalid_receipt');
    await q('UPDATE flowhub_system_commands SET receipt=$3,terminal=$4 WHERE deployment=$1 AND id=$2 AND NOT terminal',[deployment,b.receipt.id,JSON.stringify(b.receipt),TERMINAL.has(b.receipt.status)]);
   }
   // Never expire an already handed-off running command here; the device must reconcile it.
   await q(`UPDATE flowhub_system_commands SET terminal=true,receipt=jsonb_build_object('id',id,'status','expired','reason','expired_before_delivery')
       WHERE deployment=$1 AND NOT terminal AND receipt IS NULL AND (command->>'expires_at')::float8<$2`,[deployment,now]);
   const pending=(await q('SELECT command FROM flowhub_system_commands WHERE deployment=$1 AND NOT terminal ORDER BY created_at LIMIT 1',[deployment])).rows[0];
   if(pending)await q(`UPDATE flowhub_system_commands SET receipt=COALESCE(receipt,jsonb_build_object('id',id,'status','running','reason','delivered')) WHERE id=$1`,[pending.command.id]);
   return json({command:pending?.command??null,received_at:now});
  }
  const record=(await q('SELECT snapshot,extract(epoch from received_at) received_at FROM flowhub_system_deployments WHERE id=$1',[deployment])).rows[0];
  const snapshot=record?.snapshot;
  const meta={schema_version:1,deployment,source_tag:'v1.0-stable',instance_id:snapshot?.status?.instance_id??null,
    source_revision:snapshot?.status?.source_revision??null,observed_at:snapshot?.status?.observed_at??null,uploaded_at:record?Number(record.received_at):null,
    stale:!record||now-Number(record.received_at)>90||now-(snapshot?.status?.observed_at??0)>90,
    coverage:snapshot?.progress??{}};
  if(request.method==='POST'&&path==='commands'){
   const body=await request.json();
   if(!text(body?.idempotency_key))throw fail('idempotency_key_required');
   await q('SELECT pg_advisory_xact_lock(72841923)');
   const existing=(await q('SELECT command,receipt FROM flowhub_system_commands WHERE deployment=$1 AND idempotency_key=$2',[deployment,body.idempotency_key])).rows[0];
   if(existing){
    const original={...existing.command};delete original.id;delete original.actor;
    if(JSON.stringify(Object.entries(original).sort())!==JSON.stringify(Object.entries(body).sort()))throw fail('idempotency_conflict',409);
    return json({...meta,...existing,replayed:true});
   }
   // Refresh status under the command lock; do not validate a pre-lock snapshot.
   const fresh=(await q('SELECT snapshot FROM flowhub_system_deployments WHERE id=$1',[deployment])).rows[0]?.snapshot;
   validateCommand(body,fresh,now);
   const active=(await q('SELECT id FROM flowhub_system_commands WHERE deployment=$1 AND NOT terminal',[deployment])).rows[0];
   if(active)throw fail('another_command_pending',409);
   const command={...body,id:randomUUID(),actor:'anonymous'};
   await q('INSERT INTO flowhub_system_commands(id,deployment,idempotency_key,command) VALUES($1,$2,$3,$4)',[command.id,deployment,body.idempotency_key,JSON.stringify(command)]);
   return json({...meta,command,status:'queued'},202);
  }
  if(request.method!=='GET')return json({error:'method_not_allowed'},405);
  if(path==='status')return json({...meta,status:meta.stale?{...snapshot?.status,state:'unknown',last_known_state:snapshot?.status?.state}:snapshot.status});
  if(path==='activity')return json({...meta,items:snapshot?.activity??[],evidence:'valid_lease_not_proof_of_network_execution'});
  if(path.startsWith('commands/')){
   const r=(await q('SELECT command,receipt,terminal FROM flowhub_system_commands WHERE deployment=$1 AND id=$2',[deployment,path.slice(9)])).rows[0];
   return r?json({...meta,...r}):json({error:'not_found'},404);
  }
  const limit=Math.min(100,Math.max(1,Number(url.searchParams.get('limit'))||50));
  if(path==='tasks'){
   const state=url.searchParams.get('state');
   const rows=(await q(`SELECT key,body,extract(epoch from received_at) uploaded_at FROM flowhub_system_entities
     WHERE deployment=$1 AND kind='product' AND key>$2 AND ($3::text IS NULL OR body->>'state'=$3) ORDER BY key LIMIT $4`,[deployment,url.searchParams.get('cursor')??'',state,limit+1])).rows;
   return json({...meta,items:rows.slice(0,limit),next_cursor:rows.length>limit?rows[limit-1].key:null,counts:snapshot?.status?.queue_counts??null});
  }
  if(path.startsWith('products/')){
   const sku=decodeURIComponent(path.slice(9));if(!text(sku,100))throw fail('invalid_sku');
   const seller=url.searchParams.get('seller_id');
   const items=(await q(`SELECT kind,key,body FROM flowhub_system_entities WHERE deployment=$1 AND kind IN ('product','publication','success') AND body->>'sku'=$2 AND ($3::text IS NULL OR body->>'seller'=$3) LIMIT 200`,[deployment,sku,seller])).rows;
   const events=(await q(`SELECT body FROM flowhub_system_entities WHERE deployment=$1 AND kind='event' AND body->>'sku'=$2 ORDER BY (body->>'id')::bigint DESC LIMIT 50`,[deployment,sku])).rows.map(r=>r.body);
   return json({...meta,items,events,event_seller_coverage:'stage_events_do_not_always_include_seller'});
  }
  if(path==='errors'){
   const cursor=Number(url.searchParams.get('cursor')??0);if(!Number.isFinite(cursor))throw fail('invalid_cursor');
   const rows=(await q(`SELECT body FROM flowhub_system_entities WHERE deployment=$1 AND kind='event' AND body->>'error_class' IS NOT NULL AND (body->>'id')::bigint>$2 ORDER BY (body->>'id')::bigint LIMIT $3`,[deployment,cursor,limit+1])).rows.map(r=>r.body);
   const logs=(await q(`SELECT body FROM flowhub_system_entities WHERE deployment=$1 AND kind='log' ORDER BY received_at DESC LIMIT 20`,[deployment])).rows.map(r=>r.body);
   return json({...meta,items:rows.slice(0,limit),next_cursor:rows.length>limit?String(rows[limit-1].id):null,logs});
  }
  if(path==='metrics'||path==='throughput'){
   if(url.searchParams.get('tz')&&url.searchParams.get('tz')!=='Asia/Shanghai')throw fail('unsupported_timezone');
   const range=dayRange(url.searchParams.get('date')??undefined,now);
   const success=(await q(`SELECT body->>'owner' owner,body->>'sku' sku,body->>'seller' seller,
      min((body->>'first_verified_at')::float8) at FROM flowhub_system_entities
      WHERE deployment=$1 AND kind='success' AND body->>'backend'='maozi_follow'
      GROUP BY body->>'owner',body->>'sku',body->>'seller'`,[deployment])).rows;
   const stamps=success.map(r=>Number(r.at));
   const today=stamps.filter(t=>t>=range.start&&t<Math.min(range.end,now+0.001));
   const pubs=(await q(`SELECT key,body FROM flowhub_system_entities WHERE deployment=$1 AND kind='publication' AND body->>'backend'='maozi_follow'
        AND (body->>'started_at')::float8 >= $2 AND (body->>'started_at')::float8 < $3`,[deployment,range.start,Math.min(range.end,now)])).rows;
   const verified=pubs.filter(r=>success.some(s=>s.owner===r.body.owner&&s.sku===r.body.sku&&s.seller===r.body.seller&&Number(s.at)>=Number(r.body.started_at))).length;
   const coverage=snapshot?.progress?.coverage;
   const uploadPending=snapshot?.progress?.upload_pending_after_batch??{};
   const complete=Boolean(coverage?.plugin_pipeline&&coverage?.plugin_publications)&&!meta.stale&&['product','publication','success'].every(k=>!uploadPending[k]);
   const hours=Array.from({length:24},(_,h)=>({start:range.start+h*3600,seconds_observed:Math.max(0,Math.min(3600,now-range.start-h*3600)),successes:today.filter(t=>t>=range.start+h*3600&&t<range.start+(h+1)*3600).length}));
   const attempts=(await q(`SELECT body->>'module' module,count(*) attempts,
       count(*) FILTER (WHERE body->>'error_class' IS NOT NULL) errors,
       avg((body->>'duration_seconds')::float8) mean_seconds,max((body->>'duration_seconds')::float8) max_seconds
       FROM flowhub_system_entities WHERE deployment=$1 AND kind='event'
       AND (body->>'finished')::float8 >= $2 AND (body->>'finished')::float8 < $3 GROUP BY body->>'module'`,[deployment,range.start,Math.min(range.end,now)])).rows;
   return json({...meta,date:range.date,timezone:'Asia/Shanghai',complete,successes:today.length,
      last_60_minutes:stamps.filter(t=>t>now-3600&&t<=now).length,hours,
      cohort:{enrolled:pubs.length,verified,not_verified:pubs.length-verified,success_rate_including_pending:pubs.length?verified/pubs.length:null},
      observed_failures:(await q(`SELECT body->>'category' category,count(*) count FROM flowhub_system_entities WHERE deployment=$1 AND kind='failure' AND (body->>'transition_observed_at')::float8 >= $2 AND (body->>'transition_observed_at')::float8 < $3 GROUP BY body->>'category'`,[deployment,range.start,Math.min(range.end,now)])).rows,
      failures:null,failure_coverage:'terminal_failure_time_not_proven; error_attempts_are_not_failed_products',steps:attempts,
      count_unit:'unique_owner_source_sku_seller_first_verified_maozi_follow',projection_note:'cyclic_bounded_projection; see coverage timestamps'});
  }
  return json({error:'not_found'},404);
 }catch(e){if(e.status)return json({error:e.message},e.status);throw e;}
}
