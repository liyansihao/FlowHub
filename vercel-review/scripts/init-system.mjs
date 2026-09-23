import pg from 'pg';
import {systemSchema} from '../lib/system-schema.mjs';
const c=new pg.Client({connectionString:process.env.DATABASE_URL});await c.connect();
try{await c.query('BEGIN');await c.query('SELECT pg_advisory_xact_lock(72841923)');await systemSchema(c);await c.query('COMMIT');console.log('system schema ready; existing review/operations data unchanged');}
catch(e){await c.query('ROLLBACK');throw e;}finally{await c.end();}
