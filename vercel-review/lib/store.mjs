// Every request uses a database transaction. Mutations additionally hold an
// advisory lock so concurrent approvals and refreshes cannot overwrite decisions.
export class ReviewStore {
 constructor(client){this.client=client;this.cache=new Map();}
 async get(key){
  if(this.cache.has(key))return structuredClone(this.cache.get(key));
  if(key!=='snapshot')return (await this.client.query('SELECT value FROM flowhub_review_blobs WHERE key=$1',[key])).rows[0]?.value ?? null;
  const rows=(await this.client.query("SELECT key,value FROM flowhub_review_blobs WHERE key >= 'snapshot:' AND key < 'snapshot;' ORDER BY key")).rows;
  const meta=rows.find(r=>r.key==='snapshot:meta')?.value;if(!meta)return null;
  const items=rows.filter(r=>r.key.startsWith('snapshot:bucket:')).flatMap(r=>r.value);
  items.sort((a,b)=>a.sku.localeCompare(b.sku)||a.seller.localeCompare(b.seller));return {...meta,items};
 }
 async write(key,value){await this.client.query('INSERT INTO flowhub_review_blobs(key,value) VALUES($1,$2::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value WHERE flowhub_review_blobs.value IS DISTINCT FROM EXCLUDED.value',[key,JSON.stringify(value)]);}
 async setJSON(key,value){
  this.cache.delete(key);
  if(key!=='snapshot')return this.write(key,value);
  const {items,...meta}=value,buckets=Array.from({length:128},()=>[]);
  for(const item of items){let hash=0;for(const char of `${item.sku}:${item.seller}`)hash=(hash*31+char.charCodeAt(0))>>>0;buckets[hash%128].push(item);}
  const rows=buckets.map((value,i)=>({key:`snapshot:bucket:${String(i).padStart(3,'0')}`,value}));rows.push({key:'snapshot:meta',value:meta});
  await this.client.query('INSERT INTO flowhub_review_blobs(key,value) SELECT key,value FROM jsonb_to_recordset($1::jsonb) AS x(key text,value jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value WHERE flowhub_review_blobs.value IS DISTINCT FROM EXCLUDED.value',[JSON.stringify(rows)]);
 }
 async list({prefix}){const rows=(await this.client.query('SELECT key,value FROM flowhub_review_blobs WHERE starts_with(key,$1)',[prefix])).rows;for(const row of rows)this.cache.set(row.key,row.value);return {blobs:rows.map(({key})=>({key}))};}
 async importHistory(items){const rows=items.map(value=>({key:`decisions/${encodeURIComponent(value.id)}`,value}));return (await this.client.query('INSERT INTO flowhub_review_blobs(key,value) SELECT key,value FROM jsonb_to_recordset($1::jsonb) AS x(key text,value jsonb) ON CONFLICT(key) DO NOTHING',[JSON.stringify(rows)])).rowCount;}
}
