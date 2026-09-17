"""Read exact owned platform errors and resume only nonblocking-warning cases."""
import asyncio,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.control import load_runtime_env
from flowhub.listing_controls import context,online
from flowhub.plugin_publication import classify_product_issues
from scripts.drive_policy_listings import snapshot,ROOT

async def main():
 load_runtime_env();db=Database();queue=asyncio.Queue()
 for r in snapshot(db):
  if r['listing']=='blocked' and r['error']=='平台商品存在错误，需修复后才能恢复可售':queue.put_nowait(r)
 async def worker():
  while not queue.empty():
   r=queue.get_nowait();key=r['key'];result={'sku':r['sku'],'seller':r['seller'],'at':time.time()}
   try:
    with db.connect() as c:
     control=c.execute('SELECT body,updated FROM product_listing_controls WHERE owner=? AND sku=? AND seller=?',key).fetchone();b=json.loads(control['body'])
    api,cfg,record,store=context(db,key);observations=[];okay=bool(b.get('targets'))
    for target in b.get('targets',[]):
     row=await online(api,target)
     if row is None:okay=False;continue
     issues=row.get('errors') or [];codes=tuple(str(e.get('code') or 'unknown_platform_issue') for e in issues)
     blocker=classify_product_issues(codes,issues=issues,status=row.get('online_status',''),primary_image=row.get('primary_image') or '')
     observations.append({'target':target,'status':row.get('online_status'),'issues':issues,'blocker':blocker})
     if blocker:okay=False
    result.update(observations=observations,resumed=False)
    if okay:
     with db.connect() as c:
      c.execute('BEGIN IMMEDIATE')
      if not c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(*key,time.time())).fetchone() and not c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',key[:2]).fetchone():
       b.pop('error',None);b['workflow_repaired_at']=time.time()
       result['resumed']=bool(c.execute("UPDATE product_listing_controls SET state='waiting',body=?,updated=? WHERE owner=? AND sku=? AND seller=? AND state='blocked' AND updated=?",(json.dumps(b),time.time()-6,*key,control['updated'])).rowcount)
    print(json.dumps({'sku':r['sku'],'resumed':result['resumed'],'codes':[e.get('code') for o in observations for e in o['issues']]},ensure_ascii=False),flush=True)
   except Exception as e:result['error']=type(e).__name__+': '+str(e)[:180];print(json.dumps(result,ensure_ascii=False),flush=True)
   with (ROOT/'platform-errors.jsonl').open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
   queue.task_done()
 await asyncio.gather(*(worker() for _ in range(3)))
if __name__=='__main__':asyncio.run(main())
