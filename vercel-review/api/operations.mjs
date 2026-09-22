import pg from 'pg';
import {serveOperations} from '../lib/operations.mjs';
let pool;
export default async function handler(req,res){
 res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');
 if(!process.env.DATABASE_URL||!process.env.REVIEW_SYNC_TOKEN)return res.status(503).json({error:'运营服务尚未配置'});
 pool??=new pg.Pool({connectionString:process.env.DATABASE_URL,max:2,connectionTimeoutMillis:10000,idleTimeoutMillis:10000});
 let c;
 try{
  c=await pool.connect();await c.query('BEGIN');
  if(req.method!=='GET')await c.query('SELECT pg_advisory_xact_lock(72841922)');
  const body=req.method==='GET'?undefined:typeof req.body==='string'?req.body:JSON.stringify(req.body);
  const request=new Request(new URL(req.url,`https://${req.headers.host}`),{method:req.method,headers:req.headers,...(body===undefined?{}:{body})});
  const response=await serveOperations(c,request,process.env.REVIEW_SYNC_TOKEN);
  await c.query(response.status>=400?'ROLLBACK':'COMMIT');
  for(const [k,v] of response.headers)res.setHeader(k,v);
  res.status(response.status).send(await response.text());
 }catch(error){if(c)await c.query('ROLLBACK').catch(()=>{});console.error('operations_failed',error.code||error.name);res.status(503).json({error:'运营服务暂时不可用，请刷新核对处理状态'});}
 finally{c?.release();}
}
