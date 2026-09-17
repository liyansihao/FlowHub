import pg from 'pg';
const client=new pg.Client({connectionString:process.env.DATABASE_URL});await client.connect();
try{await client.query('CREATE TABLE IF NOT EXISTS flowhub_review_blobs (key TEXT PRIMARY KEY,value JSONB NOT NULL)');}finally{await client.end();}
