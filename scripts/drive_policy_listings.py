"""Drive only listing intents approved by the fixed review-rule batch."""
import asyncio,collections,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.control import load_runtime_env
from flowhub.listing_controls import tick
ROOT=Path(__file__).resolve().parents[1]/'reports/review-rule-listing-20260917'


def snapshot(db):
 manifest=json.loads((ROOT/'manifest.json').read_text());owner=manifest['owner']
 results={}
 for line in (ROOT/'results.jsonl').read_text().splitlines():
  r=json.loads(line)
  if r['result']=='listing_requested':results[(r['sku'],r['seller'])]=r
 rows=[]
 with db.connect() as c:
  for sku,seller in results:
   r=c.execute('SELECT state,body FROM product_listing_controls WHERE owner=? AND sku=? AND seller=? AND action=\'list\'',(owner,sku,seller)).fetchone()
   q=c.execute('SELECT state,body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
   if r:
    b=json.loads(r['body']);qb=json.loads(q['body'])
    rows.append({'sku':sku,'seller':seller,'listing':r['state'],'pipeline':q['state'],'phase':b.get('phase'),'publication_phase':qb.get('phase'),'next_attempt_at':b.get('next_attempt_at',0),'error':b.get('error'),'pipeline_reason':qb.get('error') or qb.get('reason'),'key':(owner,sku,seller)})
 return rows


async def main():
 load_runtime_env();db=Database();started=time.time();last=None
 while time.time()-started<300:
  rows=snapshot(db);summary=dict(collections.Counter(r['listing'] for r in rows))
  if summary!=last:print(json.dumps({'states':summary,'elapsed':round(time.time()-started)},ensure_ascii=False),flush=True);last=summary
  ready=[r for r in rows if r['listing'] in ('queued','waiting') and r['next_attempt_at']<=time.time()]
  queue=asyncio.Queue()
  ready.sort(key=lambda r:(r['listing']!='queued',r['phase']=='publication_pipeline'))
  for r in ready:queue.put_nowait(r)
  async def worker():
   while not queue.empty():
    r=queue.get_nowait()
    try:
     if r['pipeline'] in ('publishing','awaiting_remote'):
      from flowhub.plugin_pipeline import tick as pipeline_tick, RECONCILE
      lane='reconcile_history' if r['publication_phase']=='manual_review' else 'reconcile' if r['publication_phase'] in RECONCILE else 'submit'
      await asyncio.wait_for(pipeline_tick(db,lane=lane,target=r['key']),150)
     else:await asyncio.wait_for(tick(db,target=r['key']),150)
    except Exception as e:print(json.dumps({'sku':r['sku'],'error':type(e).__name__+': '+str(e)[:120]},ensure_ascii=False),flush=True)
    queue.task_done()
  await asyncio.gather(*(worker() for _ in range(2)))
  await asyncio.sleep(5)
 rows=snapshot(db)
 (ROOT/'listing-status.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
 print(json.dumps({'final':dict(collections.Counter(r['listing'] for r in rows))},ensure_ascii=False),flush=True)

if __name__=='__main__':asyncio.run(main())
