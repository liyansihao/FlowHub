"""One-hour authorized seed/evaluate/publish run; all publication writes expire."""
import asyncio,json,os,sqlite3,time
from pathlib import Path
from .db import Database,ROOT
from .maozi import MaoziPublisher
from .plugin_detail import enrich_plugin_detail
from .plugin_comparebot import evaluate
from .plugin_publication import approved,require_quota
from .source_library import SourceLibrary
from .plugin_pipeline import enqueue
from .seed_pipeline import RUN as SEEDS,OWNER
RUN=ROOT/'reports/seed-hour-20260912'

async def stores(db, output=RUN, *, base_store_id=None):
 if not base_store_id:raise ValueError('explicit base_store_id required for historical helper')
 with db.connect() as c:base=c.execute('SELECT * FROM stores WHERE owner=? AND id=?',(OWNER,base_store_id)).fetchone()
 if not base:raise ValueError('base store not found for owner')
 keys=db.open(base['secret']);p=MaoziPublisher({'store':{'config':json.loads(base['config']),'credentials':keys}})
 result=[]
 for r in await p.rows('/api.shop/lists',{'scene':'erp'}):
  if r.get('status') not in (1,'1') or r.get('currency')!='CNY':continue
  warehouses=[w for w in r.get('warehouse',[]) if '嘉兴邮政' in w.get('name','')]
  if len(warehouses)!=1 or not r.get('watermark_id'):continue
  q=await p.erp('POST','/api.shop/sync_single_product_limit',body={'id':r['id']})
  item={'shop_id':str(r['id']),'name':r['name'],'quota':q,'warehouse_id':str(warehouses[0]['warehouse_id']),'watermark_id':str(r['watermark_id'])}
  try:require_quota(q)
  except ValueError:item['eligible']=False;result.append(item);continue
  item['eligible']=True
  with db.connect() as c:
   existing=c.execute("SELECT id FROM stores WHERE owner=? AND json_extract(config,'$.shop_id')=?",(OWNER,str(r['id']))).fetchone()
   sid=existing[0] if existing else 'seed-hour-'+str(r['id'])
   if not existing:c.execute('INSERT INTO stores(id,owner,name,kind,config,secret,enabled,verified) VALUES(?,?,?,?,?,?,0,1)',(sid,OWNER,r['name'],'maozi',json.dumps({k:item[k] for k in ('shop_id','warehouse_id','watermark_id')}),db.seal({'erp_token':keys['erp_token']})))
   else:
    current=json.loads(c.execute('SELECT config FROM stores WHERE id=?',(sid,)).fetchone()[0])
    if str(current.get('warehouse_id'))!=item['warehouse_id']:item['eligible']=False
   item['store_id']=sid
  result.append(item)
 (output/'stores.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 return [s for s in result if s['eligible']],p

def snapshot(db,meta):
 with db.connect() as c:
  rows=c.execute('SELECT sku,body FROM plugin_publications').fetchall();pipelines=c.execute('SELECT sku,state,body FROM plugin_pipeline').fetchall()
 records=[json.loads(r['body']) for r in rows if json.loads(r['body']).get('run_id')==meta['run_id']]
 sold=[{'source_sku':r['sku'],'shop':r['store_name'],'product':r.get('product'),'stocks':r.get('stocks')} for r in records if r.get('verified')]
 result={**meta,'at':time.time(),'verified_selling':len(sold),'published_intents':len(records),'selling':sold,'phases':{r['sku']:r.get('phase','prepared') for r in records}}
 (RUN/'status.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 (RUN/'进度.md').write_text('# 一小时种子来源与上架验收\n\n'+f"开始：{time.strftime('%H:%M:%S',time.localtime(meta['started_at']))}；截止：{time.strftime('%H:%M:%S',time.localtime(meta['deadline']))}。\n\n本轮已回查可售 {len(sold)} 件；发布意图 {len(records)} 件。原有成功商品不计入。只操作绑定的嘉兴邮政仓，目标库存 99。到时停止新增及库存写入。\n\n"+'\n'.join(f"- 来源 {r['source_sku']} → {r['product']['sku']}，{r['shop']}" for r in sold))
 return result

async def main():
 RUN.mkdir(exist_ok=True,parents=True);db=Database()
 eligible,client=await stores(db)
 if not eligible:raise RuntimeError('no_postal_store_with_quota')
 meta={'run_id':'seed-hour-20260912','started_at':time.time(),'deadline':time.time()+3600,'stores':[s['shop_id'] for s in eligible]}
 (RUN/'run.json').write_text(json.dumps(meta));(SEEDS/'DEADLINE').write_text(str(meta['deadline']))
 (RUN/'pid').write_text(str(os.getpid()))
 with db.connect() as c:c.execute('CREATE TABLE IF NOT EXISTS plugin_routes(owner TEXT,sku TEXT,seller TEXT,store_id TEXT,expires REAL,run_id TEXT,PRIMARY KEY(owner,sku,seller))')
 seen=set();slot=0
 while time.time()<meta['deadline']:
  snapshot(db,meta)
  with sqlite3.connect(SEEDS/'progress.sqlite3') as c:rows=c.execute("SELECT sku,seller FROM items WHERE state='matched' ORDER BY updated DESC").fetchall()
  selected=next(((sku,seller) for sku,seller in rows if sku not in seen),None)
  if selected is None:await asyncio.sleep(10);continue
  sku,seller=selected;seen.add(sku)
  try:
   with db.connect() as c:
    if c.execute('SELECT 1 FROM jobs WHERE owner=? AND source_key=?',(OWNER,sku)).fetchone():continue
    if c.execute('SELECT 1 FROM plugin_publications WHERE owner=? AND sku=?',(OWNER,sku)).fetchone():continue
   # Refresh the exact SKU's sales facts before publication approval, re-evaluate if changed.
   proc=await asyncio.create_subprocess_exec('node',str(ROOT/'bridges/plugin-batch-detail.mjs'),sku,seller,str(SEEDS/'facts'),'--refresh',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
   out,_=await asyncio.wait_for(proc.communicate(),140)
   if proc.returncode or json.loads(out).get('state')!='ready':raise ValueError('publication_detail_unavailable')
   packet=json.loads((SEEDS/'facts'/f'{sku}.json').read_text())
   with db.connect() as c:p=json.loads(c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(OWNER,sku,seller)).fetchone()[0])
   p=enrich_plugin_detail(p,packet['sales'],packet['variant']);SourceLibrary(db).put(OWNER,p,{'channel':'hour-publication-recheck'})
   review=await evaluate(db,OWNER,sku,seller)
   with db.connect() as c:rules=json.loads(c.execute('SELECT rules FROM workflows WHERE owner=?',(OWNER,)).fetchone()[0])
   approved(review,rules)
   chosen=None
   for i in range(len(eligible)):
    s=eligible[(slot+i)%len(eligible)]
    q=await client.erp('POST','/api.shop/sync_single_product_limit',body={'id':int(s['shop_id'])})
    try:require_quota(q)
    except ValueError:continue
    chosen=s;slot=(slot+i+1)%len(eligible);break
   if not chosen:raise ValueError('no_remaining_store_capacity')
   if time.time()>=meta['deadline']:break
   with db.connect() as c:c.execute('INSERT OR IGNORE INTO plugin_routes VALUES(?,?,?,?,?,?)',(OWNER,sku,seller,chosen['store_id'],meta['deadline'],meta['run_id']))
   result=enqueue(db,OWNER,sku,seller)
   event={'at':time.time(),'sku':sku,'shop_id':chosen['shop_id'],'state':result['state']}
  except Exception as e:event={'at':time.time(),'sku':sku,'state':'not_admitted','reason':str(e)[:250]}
  with (RUN/'admissions.jsonl').open('a') as f:f.write(json.dumps(event,ensure_ascii=False)+'\n')
  print(json.dumps(event,ensure_ascii=False),flush=True)
  await asyncio.sleep(1)
 snapshot(db,meta)
 print('Hour window closed; new publication and stock writes disabled by persisted deadlines.',flush=True)

if __name__=='__main__':raise SystemExit('Legacy browser runner retired. Use pipeline_campaigns and python -m flowhub.pipeline_modules status.')
