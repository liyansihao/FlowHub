export async function schema(c){
 await c.query(`
 CREATE TABLE IF NOT EXISTS flowhub_review_meta (
  id smallint PRIMARY KEY CHECK(id=1),owner text NOT NULL,version bigint NOT NULL DEFAULT 1,
  cursor text NOT NULL DEFAULT '',generated_at text,viewer_at timestamptz NOT NULL DEFAULT 'epoch');
 CREATE TABLE IF NOT EXISTS flowhub_review_products (
  owner text NOT NULL,sku text NOT NULL,seller text NOT NULL,revision text NOT NULL,
  state text NOT NULL,listing_state text,search_text text NOT NULL,data jsonb NOT NULL,
  PRIMARY KEY(owner,sku,seller));
 CREATE INDEX IF NOT EXISTS review_products_state ON flowhub_review_products(owner,state,sku,seller);
 CREATE TABLE IF NOT EXISTS flowhub_review_decisions (
  id text PRIMARY KEY,owner text NOT NULL,sku text NOT NULL,seller text NOT NULL,revision text NOT NULL,
  action text NOT NULL,status text NOT NULL,created_at text NOT NULL,data jsonb NOT NULL);
 CREATE INDEX IF NOT EXISTS review_decisions_product ON flowhub_review_decisions(owner,sku,seller,revision);
 CREATE INDEX IF NOT EXISTS review_decisions_pending ON flowhub_review_decisions(owner,created_at,id) WHERE status='pending';
 CREATE INDEX IF NOT EXISTS review_decisions_history ON flowhub_review_decisions(owner,created_at DESC,id DESC);
 `);
}
