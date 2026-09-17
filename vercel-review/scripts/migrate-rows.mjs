import pg from 'pg';
import {schema} from '../lib/schema.mjs';
const c=new pg.Client({connectionString:process.env.DATABASE_URL});await c.connect();
try{
 await c.query('BEGIN');await c.query('SELECT pg_advisory_xact_lock(72841916)');await schema(c);
 const existing=await c.query('SELECT owner FROM flowhub_review_meta WHERE id=1');
 if(existing.rowCount)throw Error('normalized tables already initialized; do not overwrite');
 await c.query(`INSERT INTO flowhub_review_meta(id,owner,generated_at) SELECT 1,value->>'owner',value->>'generated_at' FROM flowhub_review_blobs WHERE key='snapshot:meta'`);
 await c.query(`INSERT INTO flowhub_review_products(owner,sku,seller,revision,state,listing_state,search_text,data)
 SELECT m.owner,x->>'sku',x->>'seller',x->>'revision',coalesce(x->>'pipeline_state',''),x->>'listing_state',lower(concat_ws(' ',x->>'sku',x->>'title',x->>'supplier_title',x->>'target_store_name',x->>'note')),x
 FROM flowhub_review_blobs b CROSS JOIN LATERAL jsonb_array_elements(b.value) x CROSS JOIN flowhub_review_meta m WHERE b.key LIKE 'snapshot:bucket:%'`);
 await c.query(`INSERT INTO flowhub_review_decisions(id,owner,sku,seller,revision,action,status,created_at,data)
 SELECT value->>'id',value->>'owner',value->>'sku',value->>'seller',value->>'revision',value->>'action',value->>'status',value->>'created_at',value FROM flowhub_review_blobs WHERE starts_with(key,'decisions/')`);
 await c.query(`INSERT INTO flowhub_review_decisions(id,owner,sku,seller,revision,action,status,created_at,data)
 SELECT x->>'id',m.owner,x->>'sku',x->>'seller',x->>'revision',x->>'action',x->>'status',x->>'created_at',x||jsonb_build_object('owner',m.owner)
 FROM flowhub_review_blobs b CROSS JOIN LATERAL jsonb_array_elements(coalesce(b.value->'local_history','[]')) x CROSS JOIN flowhub_review_meta m WHERE b.key='snapshot:meta' ON CONFLICT(id) DO NOTHING`);
 const counts=(await c.query(`SELECT (SELECT count(*) FROM flowhub_review_products)::int products,(SELECT count(*) FROM flowhub_review_decisions)::int decisions`)).rows[0];
 await c.query('COMMIT');console.log(JSON.stringify({migrated:true,old_blobs_retained:true,...counts}));
}catch(e){await c.query('ROLLBACK');throw e;}finally{await c.end();}
