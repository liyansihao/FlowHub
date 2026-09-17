// Run after the new deployment is live to retain commands written during rollout.
import pg from 'pg';
const c=new pg.Client({connectionString:process.env.DATABASE_URL});await c.connect();
try{
 await c.query('BEGIN');await c.query('SELECT pg_advisory_xact_lock(72841916)');
 const r=await c.query(`INSERT INTO flowhub_review_decisions(id,owner,sku,seller,revision,action,status,created_at,data)
 SELECT value->>'id',value->>'owner',value->>'sku',value->>'seller',value->>'revision',value->>'action',value->>'status',value->>'created_at',value FROM flowhub_review_blobs WHERE starts_with(key,'decisions/')
 ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data WHERE flowhub_review_decisions.status='pending' AND excluded.status IN ('applied','rejected')`);
 const missing=(await c.query("SELECT count(*)::int n FROM flowhub_review_blobs b WHERE starts_with(b.key,'decisions/') AND NOT EXISTS(SELECT 1 FROM flowhub_review_decisions d WHERE d.id=b.value->>'id')")).rows[0].n;
 if(missing)throw Error('legacy decisions missing');if(r.rowCount)await c.query('UPDATE flowhub_review_meta SET version=version+1 WHERE id=1');await c.query('COMMIT');console.log(JSON.stringify({reconciled:r.rowCount,missing:0}));
}catch(e){await c.query('ROLLBACK');throw e;}finally{await c.end();}
