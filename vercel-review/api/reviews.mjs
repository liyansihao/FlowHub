import pg from 'pg';
import {gunzipSync} from 'node:zlib';
import {serve} from '../lib/service.mjs';
let pool;
export default async function handler(req,res){
 res.setHeader('Cache-Control','no-store');
 if(!process.env.DATABASE_URL||!process.env.REVIEW_SYNC_TOKEN){res.status(503).json({error:'审核服务正在初始化，请稍后重试'});return;}
 pool??=new pg.Pool({connectionString:process.env.DATABASE_URL,max:3,connectionTimeoutMillis:10000,idleTimeoutMillis:10000});
 let client;
 try{
  const url=new URL(req.url,'https://review.invalid'),mode=url.searchParams.get('mode')||'review';
  if(mode!=='review'&&req.headers['x-sync-token']!==process.env.REVIEW_SYNC_TOKEN){res.status(401).json({error:'unauthorized'});return;}
  let body;
  if(req.method!=='GET'&&req.method!=='HEAD'){
   body=typeof req.body==='string'?req.body:JSON.stringify(req.body);
   if(['refresh','delta'].includes(mode)&&body!==undefined){
    const envelope=JSON.parse(body);
    if(envelope.encoding==='gzip-base64'){
     if(typeof envelope.payload!=='string'||envelope.payload.length>4000000)throw Error('invalid_payload');
     body=gunzipSync(Buffer.from(envelope.payload,'base64'),{maxOutputLength:25000000}).toString();
    }
   }
  }
  client=await pool.connect();
  // Reads use a consistent view. Only writes share the lock with refresh/delta.
  await client.query(req.method==='GET'?'BEGIN ISOLATION LEVEL REPEATABLE READ':'BEGIN');
  if(req.method!=='GET')await client.query('SELECT pg_advisory_xact_lock(72841916)');
  const request=new Request(url,{method:req.method,headers:req.headers,...(body===undefined?{}:{body})});
  const response=await serve(client,request,process.env.REVIEW_SYNC_TOKEN);
  await client.query(response.status>=400?'ROLLBACK':'COMMIT');
  if(mode==='review'&&req.method==='GET'&&response.status<400)await client.query("UPDATE flowhub_review_meta SET viewer_at=now() WHERE id=1 AND viewer_at<now()-interval '60 seconds'").catch(()=>{});
  for(const [key,value] of response.headers)res.setHeader(key,value);
  res.status(response.status);if(response.status===304)res.end();else res.send(await response.text());
 }catch(error){if(client)await client.query('ROLLBACK').catch(()=>{});console.error('review_request_failed',error.code||error.name);res.status(503).json({error:'审核服务暂时不可用，请稍后重试；请勿重复提交'});}
 finally{client?.release();}
}
