import pg from 'pg';
import {Repository} from '../lib/repository.mjs';
import fs from 'node:fs/promises';
const c=new pg.Client({connectionString:process.env.DATABASE_URL});await c.connect();
try{
 await c.query('BEGIN READ ONLY');const repo=new Repository(c),meta=await repo.meta();
 const full=(await c.query('SELECT sum(octet_length(data::text))::bigint bytes FROM flowhub_review_products')).rows[0].bytes;
 const original=c.query.bind(c);let bytes=0,queries=0;c.query=async(...args)=>{const r=await original(...args);queries++;bytes+=Buffer.byteLength(JSON.stringify(r.rows));return r;};
 const stats=await repo.stats(meta.owner);const page=await repo.products(meta.owner,{view:'queue',q:'',state:'',size:12,requestedPage:0});const pageBytes=bytes,pageQueries=queries;bytes=0;queries=0;
 const history=await repo.history(meta.owner,{q:'',size:12,requestedPage:0});const historyBytes=bytes;bytes=0;queries=0;
 await repo.meta();const versionBytes=bytes;await c.query('COMMIT');
 const report={at:new Date().toISOString(),...stats,fullSnapshotJsonBytes:Number(full),pageRows:page.items.length,pageDatabaseResultJsonBytes:pageBytes,pageQueries,historyRows:history.history.length,historyDatabaseResultJsonBytes:historyBytes,versionDatabaseResultJsonBytes:versionBytes,reductionPercent:Math.round((1-pageBytes/Number(full))*10000)/100,note:'Serialized SQL result bytes; excludes wire/TLS overhead and is not provider-billed usage.'};
 console.log(JSON.stringify(report));await fs.writeFile('../reports/review-quota-optimization-20260916/production-read.json',JSON.stringify(report,null,2));
}finally{await c.end();}
