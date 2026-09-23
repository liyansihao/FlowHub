export async function systemSchema(c){
 await c.query(`CREATE TABLE IF NOT EXISTS flowhub_system_deployments(
   id text PRIMARY KEY, snapshot jsonb NOT NULL, received_at timestamptz NOT NULL DEFAULT now())`);
 await c.query(`CREATE TABLE IF NOT EXISTS flowhub_system_entities(
   deployment text NOT NULL, kind text NOT NULL, key text NOT NULL, body jsonb NOT NULL,
   received_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(deployment,kind,key))`);
 await c.query(`CREATE INDEX IF NOT EXISTS flowhub_system_product_sku ON flowhub_system_entities(deployment,kind,(body->>'sku'))`);
 await c.query(`CREATE TABLE IF NOT EXISTS flowhub_system_commands(
   id text PRIMARY KEY, deployment text NOT NULL, idempotency_key text NOT NULL,
   command jsonb NOT NULL, receipt jsonb, terminal boolean NOT NULL DEFAULT false,
   created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(deployment,idempotency_key))`);
 await c.query(`CREATE UNIQUE INDEX IF NOT EXISTS flowhub_system_one_command ON flowhub_system_commands(deployment) WHERE NOT terminal`);
}
