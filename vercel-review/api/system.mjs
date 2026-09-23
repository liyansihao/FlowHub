import pg from 'pg';
import {authorize,serveSystem} from '../lib/system.mjs';
let pool;
export default async function handler(req,res){
 res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');
 if(!process.env.DATABASE_URL||!process.env.SYSTEM_DEPLOYMENT_ID||!process.env.SYSTEM_AGENT_TOKEN)
  return res.status(503).json({error:'system_not_configured'});
 let c;
 try{
  const origin=process.env.SYSTEM_PUBLIC_ORIGIN;
  const path=req.query?.path;
  const url=path?`${origin}/api/system/v1/${Array.isArray(path)?path.join('/'):path}${req.url.includes('?')?'?'+req.url.split('?').slice(1).join('?'):''}`:new URL(req.url,origin).href;
  const body=req.method==='GET'?undefined:typeof req.body==='string'?req.body:JSON.stringify(req.body);
  if(body?.length>3_000_000)return res.status(413).json({error:'payload_too_large'});
  const request=new Request(url,{method:req.method,headers:req.headers,...(body===undefined?{}:{body})});
  authorize(request,process.env);
  pool??=new pg.Pool({connectionString:process.env.DATABASE_URL,max:2,connectionTimeoutMillis:10000,idleTimeoutMillis:10000});
  c=await pool.connect();await c.query('BEGIN');await c.query("SET LOCAL statement_timeout='10s'");
  const response=await serveSystem(c,request,process.env);
  await c.query(response.status>=400?'ROLLBACK':'COMMIT');
  res.status(response.status).send(await response.text());
 }catch(e){if(c)await c.query('ROLLBACK').catch(()=>{});res.status(e.status??503).json({error:e.status?e.message:'system_temporarily_unavailable'});}
 finally{c?.release();}
}
