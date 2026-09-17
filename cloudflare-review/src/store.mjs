// Fixed SKU buckets keep an individual product update from rewriting the entire snapshot.
export class ReviewStore {
 constructor(storage){this.storage=storage;this.sql=storage.sql;this.cache=null;this.sql.exec('CREATE TABLE IF NOT EXISTS review_blobs (key TEXT PRIMARY KEY, value TEXT NOT NULL)');}
 warm(){if(this.cache===null)this.cache=new Map([...this.sql.exec('SELECT key,value FROM review_blobs')].map(row=>[row.key,row.value]));}
 read(key){this.warm();return this.cache.get(key);}
 transaction(fn){try{return this.storage.transactionSync(fn);}catch(error){this.cache=null;throw error;}}
 write(key,value){if(this.read(key)!==value){this.sql.exec('INSERT INTO review_blobs(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',key,value);this.cache.set(key,value);}}
 async get(key){
  if(key!=='snapshot'){const data=this.read(key);return data?JSON.parse(data):null;}
  const header=this.read('snapshot:meta');if(!header)return null;
  const meta=JSON.parse(header),items=[];
  for(const [key,value] of this.cache)if(key.startsWith('snapshot:bucket:'))items.push(...JSON.parse(value));
  items.sort((a,b)=>a.sku.localeCompare(b.sku)||a.seller.localeCompare(b.seller));
  return {...meta,items};
 }
 async setJSON(key,value){
  if(key!=='snapshot'){this.write(key,JSON.stringify(value));return;}
  const {items,...meta}=value,buckets=Array.from({length:128},()=>[]);
  for(const item of items){let hash=0;for(const char of `${item.sku}:${item.seller}`)hash=(hash*31+char.charCodeAt(0))>>>0;buckets[hash%128].push(item);}
  this.transaction(()=>{for(let i=0;i<buckets.length;i++)this.write(`snapshot:bucket:${String(i).padStart(3,'0')}`,JSON.stringify(buckets[i]));this.write('snapshot:meta',JSON.stringify(meta));});
 }
 async list({prefix}){this.warm();return {blobs:[...this.cache.keys()].filter(key=>key.startsWith(prefix)).map(key=>({key}))};}
}
