"""Import this batch's uncertain results into the existing hosted review queue."""
import asyncio,fcntl,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.control import load_runtime_env
from flowhub.sellable_audit_policy import schema,put
from flowhub.remote_reviews import once
OUT=Path(__file__).resolve().parents[1]/'reports/sellable-identity-20260916'
async def main():
 lock=(OUT/'uncertain-sync.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 load_runtime_env();db=Database();schema(db);owner=os.environ['FLOWHUB_REVIEW_OWNER']
 inventory={str(r['shop_id'])+':'+str(r['id']):r for r in json.loads((OUT/'manifest.json').read_text())}
 while True:
  status={'at':time.time(),'imported':0,'uncertain':0}
  try:
   latest={}
   for line in (OUT/'results.jsonl').read_text().splitlines():
    try:r=json.loads(line)
    except json.JSONDecodeError:continue
    latest[r['key']]=r
   for key,r in latest.items():
    if r['verdict']!='uncertain':continue
    status['uncertain']+=1;p=inventory[key]
    identity=json.loads((OUT/f"evidence-{key.replace(':','-')}.json").read_text())
    query=identity['comparison']['search_and_rank']['query']
    if identity['verdict']!='uncertain' or str(query['product_id'])!=str(p['sku']) or query['title']!=p['name'] or query['image_url']!=p['primary_image']:raise ValueError('comparison_identity_mismatch')
    status['imported']+=int(put(db,owner,p,identity))
   with db.connect() as c:
    status['local_pending']=c.execute("SELECT count(*) FROM sellable_audit_cards WHERE owner=? AND state='needs_review' AND json_extract(body,'$.identity.verdict')='uncertain'",(owner,)).fetchone()[0]
   await once(db,refresh=True);status['sync_attempted']=True
  except Exception as e:status['error']=type(e).__name__+': '+str(e)[:200]
  (OUT/'uncertain-sync-status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2));print(json.dumps(status,ensure_ascii=False),flush=True)
  if '--once' in sys.argv:return
  await asyncio.sleep(60)
if __name__=='__main__':asyncio.run(main())
