import asyncio,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.control import load_runtime_env
from flowhub.listing_controls import schema,tick
from flowhub.manual_reviews import load,revision,decide
load_runtime_env();db=Database();schema(db);owner=os.environ['FLOWHUB_REVIEW_OWNER'];out=Path('reports/review-listing-control-20260915')
if '--enqueue' in sys.argv:
 results=[]
 with db.connect() as c:rows=[dict(r) for r in c.execute("SELECT sku,seller,json_extract(body,'$.identity_review.verdict') verdict FROM plugin_reviews WHERE owner=? AND json_extract(body,'$.identity_review.verdict') IN ('match','mismatch')",(owner,))]
 for r in rows:
  try:
   with db.connect() as c:
    old=c.execute('SELECT state FROM product_listing_controls WHERE owner=? AND sku=? AND seller=?',(owner,r['sku'],r['seller'])).fetchone()
    if old and old['state'] not in ('blocked',):results.append(r|{'state':'existing_command'});continue
    v=revision(load(c,owner,r['sku'],r['seller']))
   action='list' if r['verdict']=='match' else 'unlist'
   result=decide(db,owner,'Codex',r['sku'],r['seller'],action,'用户本轮明确要求：同款上架、不同款下架；保留网站上下架操作权',v)
   results.append(r|result)
  except Exception as e:results.append(r|{'error':str(e)[:200]})
 (out/'dispatch.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print('commands',len(results),'errors',sum('error' in x for x in results),flush=True)
async def work():
 while True:
  with db.connect() as c:active=c.execute("SELECT count(*) FROM product_listing_controls WHERE state IN ('queued','running','waiting')").fetchone()[0]
  if not active:return
  await tick(db);await asyncio.sleep(.5)
if '--run' in sys.argv:asyncio.run(asyncio.gather()) if False else asyncio.run(work())
