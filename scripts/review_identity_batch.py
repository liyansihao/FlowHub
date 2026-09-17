"""Owner-requested price supplement and same-product-only review; never publishes."""
import argparse,asyncio,collections,json,os,secrets,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.control import load_runtime_env
from flowhub.evaluation_requirements import review_sale_price
from flowhub.identity_review import evaluate,envelope,bound,comparison
from flowhub.manual_reviews import publication_started,decide,load,revision

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'reports/identity-review-20260915'
OLD=ROOT/'reports/review-supplement-20260915'
load_runtime_env();db=Database();owner=os.environ['FLOWHUB_REVIEW_OWNER']
plan=json.loads((OLD/'manifest.json').read_text());scope={(x['sku'],x['seller']) for x in plan['selected']}
receipts=collections.defaultdict(list)
for line in (OLD/'results.jsonl').read_text().splitlines():
 r=json.loads(line);receipts[(r['sku'],r['seller'])]+=r.get('steps',[])

def prepare(apply=False):
 stats=collections.Counter();selected=[];backup=[]
 with db.connect() as c:
  c.execute('BEGIN IMMEDIATE')
  rows=c.execute("SELECT q.*,p.body product,r.body review FROM plugin_pipeline q JOIN sourcing_products p USING(owner,sku,seller) LEFT JOIN plugin_reviews r USING(owner,sku,seller) WHERE q.owner=? AND q.state IN ('needs_review','needs_fields')",(owner,)).fetchall()
  for row in rows:
   sku,seller=row['sku'],row['seller'];key=(owner,sku,seller);q=json.loads(row['body']);p=json.loads(row['product']);r=json.loads(row['review'] or '{}')
   if publication_started(c,*key,q):stats['excluded_publication']+=1;continue
   if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone():stats['blocked']+=1;continue
   if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(*key,time.time())).fetchone():stats['leased']+=1;continue
   backup.append(dict(row));q['same_product_only']=True
   # All cards use the new isolated review contract. Only the prior supplement
   # manifest is re-reviewed by this fixed batch.
   if (sku,seller) in scope:
    quote=review_sale_price(p)
    if quote is None:
     for step in reversed(receipts[(sku,seller)]):
      if step.get('source')=='acquisition_reference_only':
       test={**p,'ordinary_sale_price':{'value':step.get('value'),'currency':step.get('currency'),'observed_at':step.get('observed_at'),'source':'retained-asking-price'}}
       quote=review_sale_price(test)
       if quote:p=test;break
    if quote:
     p['review_price_basis']={**quote,'usable_for_review':True,'selected_at':time.time()}
     if quote['kind']=='ordinary_sale_price':p['ordinary_sale_price']={k:v for k,v in quote.items() if k in ('value','currency','observed_at','source','raw')}
     stats['price_ready']+=1;stats[quote['kind']]+=1
    else:stats['price_missing']+=1
    try:
     candidate=envelope(p);cb=comparison(r)
     reusable=bound(cb,candidate) and bool((cb.get('decision') or {}).get('qwen_review') or (cb.get('decision') or {}).get('outcome')=='approved')
     stats['comparison_reusable' if reusable else 'comparison_requires_request']+=1
     selected.append({'sku':sku,'seller':seller,'price_ready':quote is not None})
    except ValueError:stats['identity_missing']+=1
   if apply:
    c.execute('UPDATE sourcing_products SET body=? WHERE owner=? AND sku=? AND seller=?',(json.dumps(p),*key))
    c.execute("UPDATE plugin_pipeline SET state='needs_review',body=? WHERE owner=? AND sku=? AND seller=?",(json.dumps(q),*key))
   stats['review_cards_migrated']+=1
  if apply:
   OUT.mkdir(exist_ok=True);(OUT/'before.json').write_text(json.dumps(backup,ensure_ascii=False));os.chmod(OUT/'before.json',0o600)
   (OUT/'manifest.json').write_text(json.dumps({'owner':owner,'selected':selected,'stats':stats},ensure_ascii=False,indent=2))
 print(json.dumps(stats,ensure_ascii=False),flush=True)

async def run(workers):
 items=json.loads((OUT/'manifest.json').read_text())['selected'];done=set()
 output=OUT/'results.jsonl'
 if output.exists():
  for line in output.read_text().splitlines():
   r=json.loads(line)
   if not r.get('error'):done.add((r['sku'],r['seller']))
 queue=asyncio.Queue()
 for item in items:
  if (item['sku'],item['seller']) not in done:queue.put_nowait(item)
 async def worker():
  while not queue.empty():
   item=queue.get_nowait();key=(owner,item['sku'],item['seller']);token='identity-'+secrets.token_hex(8);start=time.time();result=dict(item)
   with db.connect() as c:
    c.execute('BEGIN IMMEDIATE');row=load(c,*key)
    if row['state']!='needs_review' or publication_started(c,*key,json.loads(row['queue'])):queue.task_done();continue
    if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(*key,time.time())).fetchone():queue.task_done();continue
    c.execute('INSERT OR REPLACE INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(*key,token,time.time()+360))
   try:
    report=await asyncio.wait_for(evaluate(db,*key),280)
    identity=report['identity_review'];result.update(verdict=identity['verdict'],reused=identity['reused_comparison'])
   except Exception as e:result['error']=type(e).__name__+': '+str(e)[:200]
   finally:
    with db.connect() as c:c.execute('DELETE FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token))
   if result.get('verdict') in ('match','mismatch'):
    try:
     with db.connect() as c:v=revision(load(c,*key))
     action='approve' if result['verdict']=='match' else 'reject'
     result['decision']=decide(db,owner,'Codex 同款复核',item['sku'],item['seller'],action,
       '按用户要求仅复核同款：已核对绑定商品的图像识别证据，结果为'+('同款' if action=='approve' else '不同款')+'；价格、利润和上架资料不参与本次结论。',v)
    except Exception as e:result['error']=type(e).__name__+': '+str(e)[:200]
   result['elapsed']=round(time.time()-start,2)
   with output.open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
   print(json.dumps(result,ensure_ascii=False),flush=True);queue.task_done()
 await asyncio.gather(*(worker() for _ in range(workers)))

p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');p.add_argument('--apply',action='store_true');p.add_argument('--run',action='store_true');p.add_argument('--workers',type=int,default=2);args=p.parse_args()
if args.prepare:prepare(args.apply)
if args.run:asyncio.run(run(args.workers))
