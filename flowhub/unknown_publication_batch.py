"""Finite, separately audited continuation authorized for unknown shipping/follow facts."""
import asyncio,json,time
from .db import Database,ROOT
from .hour_acceptance import stores
from .plugin_publication import approved
from .plugin_pipeline import enqueue
from .source_delists import read_delists
from .seed_pipeline import OWNER
RUN=ROOT/'reports/unknown-publication-20260912'

async def main():
 RUN.mkdir(parents=True,exist_ok=True)
 if (RUN/'run.json').exists():raise RuntimeError('batch_already_created; resume existing queue')
 db=Database();eligible,_=await stores(db,RUN)
 if not eligible:raise RuntimeError('no_postal_store_with_quota')
 blocked=await read_delists()
 events=[json.loads(line) for line in (ROOT/'reports/seed-hour-20260912/admissions.jsonl').read_text().splitlines()]
 skus=sorted({e['sku'] for e in events if 'publication_checks_required' in e.get('reason','')})
 meta={'run_id':RUN.name,'started_at':time.time(),'deadline':time.time()+3600,'authorization':'配送模式／跟卖限制未确认的也要上架','scope':skus,'original_hour_unchanged':True}
 (RUN/'run.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2))
 results=[]
 with db.connect() as c:
  c.execute('CREATE TABLE IF NOT EXISTS plugin_publication_permissions(owner TEXT,sku TEXT,seller TEXT,expires REAL,reason TEXT,PRIMARY KEY(owner,sku,seller))')
  rules=json.loads(c.execute('SELECT rules FROM workflows WHERE owner=?',(OWNER,)).fetchone()[0])
  for sku in skus:
   try:
    if sku in blocked['skus']:raise ValueError('explicit_delist')
    if c.execute('SELECT 1 FROM plugin_publications WHERE owner=? AND sku=?',(OWNER,sku)).fetchone():raise ValueError('existing_publication')
    if c.execute('SELECT 1 FROM jobs WHERE owner=? AND source_key=?',(OWNER,sku)).fetchone():raise ValueError('existing_job')
    row=c.execute('SELECT seller,body FROM plugin_reviews WHERE owner=? AND sku=?',(OWNER,sku)).fetchone()
    if not row:raise ValueError('review_missing')
    review=json.loads(row['body']);approved(review,rules,allow_unknown=True)
    s=eligible[len([r for r in results if r['state']=='admitted'])%len(eligible)]
    expiry=min(meta['deadline'],review['finished_at']+21600)
    c.execute('INSERT OR REPLACE INTO plugin_routes VALUES(?,?,?,?,?,?)',(OWNER,sku,row['seller'],s['store_id'],expiry,meta['run_id']))
    c.execute('INSERT OR REPLACE INTO plugin_publication_permissions VALUES(?,?,?,?,?)',(OWNER,sku,row['seller'],expiry,meta['authorization']))
    results.append({'sku':sku,'seller':row['seller'],'shop_id':s['shop_id'],'state':'admitted','at':time.time(),'original_blockers':review.get('publication_blockers')})
   except ValueError as e:results.append({'sku':sku,'state':'blocked','reason':str(e),'at':time.time()})
 for r in results:
  if r['state']=='admitted':r['queue']=enqueue(db,OWNER,r['sku'],r['seller'])
 (RUN/'admissions.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
 print(json.dumps({'run_id':RUN.name,'deadline':meta['deadline'],'admitted':sum(r['state']=='admitted' for r in results),'blocked':[r for r in results if r['state']=='blocked']},ensure_ascii=False))

if __name__=='__main__':raise SystemExit('Historical fixed batch retired. Use pipeline_campaigns and python -m flowhub.pipeline_modules status.')
