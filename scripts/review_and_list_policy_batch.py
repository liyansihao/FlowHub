"""Apply the owner's DINO/Qwen rule to a frozen review cohort and enqueue passing listings."""
import asyncio,collections,json,os,secrets,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.control import load_runtime_env
from flowhub.identity_review import evaluate,comparison,envelope,bound,verdict
from flowhub.manual_reviews import load,revision,decide
from flowhub.remote_reviews import settings

ROOT=Path(__file__).resolve().parents[1]/'reports/review-rule-listing-20260917'
ACTOR='Codex 规则批量上架 20260917'
NOTE='用户明确要求检查待审核列表，按 DINO／千问规则将通过的商品上架；本次同款结论由规则计算。'


def private_save(path,value):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False)


def prepare(db):
 path=ROOT/'manifest.json'
 if path.exists():return json.loads(path.read_text())
 snapshot=json.loads((ROOT/'website-queue-stable.json').read_text());owner=settings(db)['FLOWHUB_REVIEW_OWNER']
 if snapshot['owner']!=owner:raise ValueError('owner mismatch')
 items={(x['sku'],x['seller']):{'sku':x['sku'],'seller':x['seller'],'scope':'website_queue'} for x in snapshot['items']}
 with db.connect() as c:
  for row in c.execute("SELECT q.sku,q.seller FROM plugin_pipeline q JOIN plugin_reviews r USING(owner,sku,seller) WHERE q.owner=? AND q.state='same_product_confirmed' AND json_extract(r.body,'$.identity_review.automatic_review') IS NOT NULL",(owner,)):
   items.setdefault((row['sku'],row['seller']),{'sku':row['sku'],'seller':row['seller'],'scope':'automatic_pass_waiting_listing'})
 result={'owner':owner,'created_at':time.time(),'items':list(items.values())};private_save(path,result);return result


async def one(db,owner,item):
 key=(owner,item['sku'],item['seller']);token='rule-batch-'+secrets.token_hex(12)
 if item['seller'].startswith('owned-audit:'):
  with db.connect() as c:
   row=c.execute('SELECT state,body FROM sellable_audit_cards WHERE owner=? AND sku=? AND seller=?',key).fetchone()
  if not row:return {'result':'missing'}
  b=json.loads(row['body']);v=verdict(b['identity']['comparison'])
  # These are already-owned inventory. Neither uncertain nor mismatch authorizes a new listing.
  return {'result':'owned_inventory_'+v,'state':row['state']}
 with db.connect() as c:
  c.execute('BEGIN IMMEDIATE');row=load(c,*key);q=json.loads(row['queue']);r=json.loads(row['review'] or '{}')
  if row['state'] not in ('needs_review','same_product_confirmed'):return {'result':'state_changed','state':row['state']}
  if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',key[:2]).fetchone():return {'result':'blocked_by_existing_rule'}
  if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(*key,time.time())).fetchone():return {'result':'busy'}
  control=row.get('listing_control') or {}
  if control.get('state') in ('queued','running','waiting','hold_queued','hold_waiting'):return {'result':'existing_command','action':control.get('action')}
  product=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()
  if not product:return {'result':'missing_product'}
  p=json.loads(product[0]);candidate=envelope(p);cb=(r.get('identity_review') or {}).get('comparison') or comparison(r)
  valid=bound(cb,candidate);v=verdict(cb)
  if valid and v in ('uncertain','no_candidates') and (v=='no_candidates' or (cb.get('decision') or {}).get('qwen_review')):
   return {'result':'manual_review','verdict':v}
  backup=ROOT/'before'/f"{item['sku']}-{item['seller']}.json";backup.parent.mkdir(exist_ok=True)
  if not backup.exists():private_save(backup,{'row':row,'product':p})
  publication=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',key).fetchone()
  c.execute('INSERT OR REPLACE INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(*key,token,time.time()+120))
 async def renew():
  while True:
   await asyncio.sleep(30)
   with db.connect() as c:c.execute('UPDATE plugin_pipeline_leases SET expires=? WHERE owner=? AND sku=? AND seller=? AND token=?',(time.time()+120,*key,token))
 heartbeat=asyncio.create_task(renew())
 try:
  evaluated=await asyncio.wait_for(evaluate(db,*key),240)
  identity=evaluated['identity_review'];v=identity['verdict']
  with db.connect() as c:
   c.execute('BEGIN IMMEDIATE')
   if not c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token)).fetchone():raise ValueError('lease_changed')
   latest=load(c,*key);qr=json.loads(latest['queue']);rr=json.loads(latest['review']);qr.update(same_product_only=True,reason=evaluated['reason'])
   # Publication journals retain their original approved report state; identity is an independent field.
   if publication:
    rr['state']=r.get('state','matched')
    c.execute('UPDATE plugin_reviews SET state=?,body=? WHERE owner=? AND sku=? AND seller=?',(rr['state'],json.dumps(rr),*key))
   c.execute('UPDATE plugin_pipeline SET state=?,body=? WHERE owner=? AND sku=? AND seller=?',(evaluated['state'],json.dumps(qr),*key))
 finally:
  heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
  with db.connect() as c:c.execute('DELETE FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token))
 if v!='match':return {'result':'manual_review' if v in ('uncertain','no_candidates') else 'rejected','verdict':v}
 with db.connect() as c:
  row=load(c,*key);rr=json.loads(row['review']);p=json.loads(c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0]);ib=rr['identity_review']['comparison']
  if not bound(ib,envelope(p)) or verdict(ib)!='match':raise ValueError('comparison_changed_before_listing')
  if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',key[:2]).fetchone():return {'result':'blocked_by_existing_rule'}
  version=revision(row)
 result=decide(db,owner,ACTOR,item['sku'],item['seller'],'list',NOTE,version)
 return {'result':'listing_requested','state':result['state'],'review_reused':identity['reused_comparison']}


async def main():
 load_runtime_env();db=Database();manifest=prepare(db);owner=manifest['owner'];out=ROOT/'results.jsonl';done=set()
 if out.exists():
  for line in out.read_text().splitlines():
   r=json.loads(line)
   if r['result'] not in ('busy','error'):done.add((r['sku'],r['seller']))
 queue=asyncio.Queue()
 for x in manifest['items']:
  if (x['sku'],x['seller']) not in done:queue.put_nowait(x)
 counts=collections.Counter();print(json.dumps({'cohort':len(manifest['items']),'remaining':queue.qsize()}),flush=True)
 async def worker():
  while not queue.empty():
   item=queue.get_nowait();start=time.time()
   try:result=await one(db,owner,item)
   except Exception as e:result={'result':'error','error':type(e).__name__+': '+str(e)[:220]}
   record={**item,**result,'at':time.time(),'seconds':round(time.time()-start,2)}
   fd=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
   with os.fdopen(fd,'a') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
   counts[result['result']]+=1
   if result['result'] in ('listing_requested','error','busy') or sum(counts.values())%50==0:print(json.dumps(record if result['result'] in ('listing_requested','error','busy') else dict(counts),ensure_ascii=False),flush=True)
   queue.task_done()
 await asyncio.gather(*(worker() for _ in range(3)))
 print(json.dumps({'finished':dict(counts)},ensure_ascii=False),flush=True)


if __name__=='__main__':asyncio.run(main())
