"""Bounded producer/consumer: collect complete SKU facts before evaluation admission."""
import asyncio,csv,fcntl,json,os,signal,sqlite3,time
from pathlib import Path
from .db import Database,ROOT
from .plugin_detail import enrich_plugin_detail,positive
from .plugin_comparebot import evaluate,candidate
from .source_library import SourceLibrary
from .source_acquisition import erp_request
from .evaluation_requirements import sale_price,publication_blockers

RUN=ROOT/'reports/seed-pipeline-20260912'
OWNER='local-owner'  # Historical helpers only; production uses the authenticated owner.
stop=False
collection_turn=0

def repair_schema(c):
 cols={r[1] for r in c.execute('PRAGMA table_info(items)')}
 for k,t in [('repair_attempts','INTEGER DEFAULT 0'),('repair_due','REAL DEFAULT 0')]:
  if k not in cols:c.execute(f'ALTER TABLE items ADD COLUMN {k} {t}')
 c.execute('CREATE TABLE IF NOT EXISTS repair_events(sku TEXT,at REAL,attempt INTEGER,reason TEXT,evidence TEXT)')

def repair_delay(attempt):
 return min(3600,60*2**min(max(attempt-1,0),6))

def connect():
 c=sqlite3.connect(RUN/'progress.sqlite3',timeout=30);c.row_factory=sqlite3.Row;return c

def fields(product):
 p=product.get('plugin_detail') or {};s=p.get('monthly_sales') or {};missing=[]
 for k in ('title','image','url'):
  if not product.get(k):missing.append(k)
 if not positive(p.get('weight_g')):missing.append('weight_g')
 if len(p.get('dimensions_mm') or [])!=3 or not all(positive(x) for x in p.get('dimensions_mm',[])):missing.append('dimensions_mm')
 if not sale_price(product):missing.append('sale_price_with_currency')
 return missing

def merge_cached_sales(product,packet):
 observation=packet.get('maozi') or {};response=observation.get('result') or {}
 row=response.get('data') or {};flags=response.get('status') or {}
 # The plugin cache uses the same monthly sales fields. Only accept a complete,
 # exact seller/SKU record explicitly marked current by Maozi.
 if flags.get('update_sales') is not False:return False
 if str(row.get('sku'))!=str(product['sku']) or str(row.get('sellerId'))!=str(product['seller_id']):return False
 if not positive(row.get('avgPrice')) or not row.get('salesSchema') or type(row.get('blockedBySeller')) is not bool:return False
 if product.get('plugin_detail',{}).get('monthly_sales'):return False
 product['plugin_detail']['monthly_sales']={'observed_at':observation['observed_at'],'period':'monthly','average_price_rub':row['avgPrice'],'sales_schema':row['salesSchema'],'blocked_by_seller':row['blockedBySeller'],'sold_count':row.get('soldCount'),'category_name':row.get('category3'),'brand':row.get('brand'),'source':'maozi-sku3-cache'}
 return True

def status():
 with connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM items ORDER BY position')]
 counts={}
 for r in rows:counts[r['state']]=counts.get(r['state'],0)+1
 active=('pending','collecting','ready','evaluating','needs_fields','repair_pending','repairing','error')
 report={'at':time.time(),'total':len(rows),'counts':counts,'remaining':sum(counts.get(k,0) for k in active),'evaluated':sum(counts.get(k,0) for k in ('matched','rejected','needs_review'))}
 (RUN/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 (RUN/'进度.md').write_text('# 种子来源采集与测算\n\n'+f"更新时间 {time.strftime('%Y-%m-%d %H:%M:%S')}；总候选 {len(rows)}，已测算 {report['evaluated']}，仍在流程中 {report['remaining']}。\n\n"+'\n'.join(f'- {k}：{v}' for k,v in counts.items())+'\n\n采集与测算独立运行，最多保留 3 件待测或测算中的完整资料。资料不齐仍保存已取到的详情和逐字段缺项，未进入匹配不计测算失败。matched 通过、rejected 未通过、needs_review 需人工核对、repair_pending 等待补采、repairing 正在补采；补采项始终计入未完成。每次缺项重试间隔从 1 分钟逐步延长到最多 1 小时，避免空转。当前批次仅测算。\n')
 with (RUN/'results.csv').open('w',encoding='utf-8-sig') as f:
  w=csv.writer(f);w.writerow(['sku','seller','state','missing_or_reason','collection_seconds','evaluation_seconds','purchase_cny','profit_pct'])
  for row in rows:
   r=json.loads(row['report'] or '{}').get('result',{});w.writerow([row['sku'],row['seller'],row['state'],row['reason'],row['collect_seconds'],row['eval_seconds'],r.get('purchase'),r.get('evidence',{}).get('profit',{}).get('assessment',{}).get('erp_profit_rate_pct')])
 return report

def paused():
 if stop or (RUN/'PAUSE').exists():return True
 deadline=RUN/'DEADLINE'
 return deadline.exists() and time.time()>=float(deadline.read_text())

def take(phase,working):
 with connect() as c:
  c.execute('BEGIN IMMEDIATE')
  row=c.execute('SELECT * FROM items WHERE state=? ORDER BY position LIMIT 1',(phase,)).fetchone()
  if row:c.execute('UPDATE items SET state=?,updated=? WHERE sku=?',(working,time.time(),row['sku']))
 return row

def take_collection():
 global collection_turn
 with connect() as c:
  c.execute('BEGIN IMMEDIATE')
  preferred='repair_pending' if collection_turn%3==0 else 'pending'
  row=c.execute("SELECT * FROM items WHERE state='pending' OR (state='repair_pending' AND repair_due<=?) ORDER BY CASE WHEN state=? THEN 0 ELSE 1 END,repair_due,position LIMIT 1",(time.time(),preferred)).fetchone()
  if row:
   collection_turn+=1
   c.execute('UPDATE items SET state=?,updated=? WHERE sku=?',('repairing' if row['state']=='repair_pending' else 'collecting',time.time(),row['sku']))
 return row

async def collect(db,row):
 sku=row['sku'];seller=row['seller']
 repair=row['state']=='repair_pending'
 proc=await asyncio.create_subprocess_exec('node',str(ROOT/'bridges/plugin-batch-detail.mjs'),sku,seller,str(RUN/'facts'),*(['--refresh'] if repair else []),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
 try:out,_=await asyncio.wait_for(proc.communicate(),140)
 except TimeoutError:
  proc.kill();await proc.wait();raise ValueError('detail_deadline')
 if proc.returncode:raise ValueError('detail_process_failed')
 result=json.loads(out);packet=json.loads((RUN/'facts'/f'{sku}.json').read_text())
 with db.connect() as c:
  source=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(OWNER,sku,seller)).fetchone()
  if not source:raise ValueError('source_missing')
  product=json.loads(source[0])
 if result['state']=='ready':product=enrich_plugin_detail(product,packet['sales'],packet['variant'])
 missing=fields(product)
 # Inspect the original Maozi direct channel as well, preserving the response
 # separately; cache update flags are not evidence of zero sales or restrictions.
 if missing and (repair or not packet.get('maozi_checked_at')):
  with db.connect() as c:secret=c.execute('SELECT secret FROM sourcing_settings WHERE owner=?',(OWNER,)).fetchone()
  try:
   cached=await erp_request('/api.chrome/sku3',{'sku':sku},db.open(secret[0])['erp_token'])
   if packet.get('maozi'):
    with (RUN/'facts'/f'{sku}.maozi-history.jsonl').open('a') as history:history.write(json.dumps(packet['maozi'])+'\n')
   packet['maozi']={'observed_at':time.time(),'result':cached}
  except Exception as e:packet['maozi']={'observed_at':time.time(),'error':type(e).__name__}
  packet['maozi_checked_at']=time.time();(RUN/'facts'/f'{sku}.json').write_text(json.dumps(packet))
 if product.get('plugin_detail'):merge_cached_sales(product,packet)
 missing=fields(product)
 evidence={'contract':'seed-collection-v3','observed_at':time.time(),'missing_fields':missing,'publication_blockers':publication_blockers(product),'detail_error':result.get('error'),'evidence_file':str(RUN/'facts'/f'{sku}.json'),'price_basis':sale_price(product)}
 product['collection_evidence']=evidence
 # Persist partial detail even when sales data is absent; no early return discards it.
 SourceLibrary(db).put(OWNER,product,{'channel':'seed-complete-collection','observed_at':time.time()})
 if result['state']!='ready':return 'needs_fields',json.dumps({'missing':missing,'error':result.get('error')}),evidence
 if missing:return 'needs_fields',json.dumps(missing),evidence
 try:candidate(product)
 except ValueError as e:return 'needs_fields',str(e),evidence
 return 'ready','',evidence

async def produce(db):
 while not paused():
  with connect() as c:queued=c.execute("SELECT count(*) FROM items WHERE state IN ('ready','evaluating')").fetchone()[0]
  if queued>=3:await asyncio.sleep(1);continue
  row=take_collection()
  if not row:
   with connect() as c:waiting=c.execute("SELECT count(*) FROM items WHERE state='repair_pending'").fetchone()[0]
   if not waiting:return
   await asyncio.sleep(5);continue
  begin=time.time()
  try:phase,reason,evidence=await collect(db,row)
  except Exception as e:phase,reason,evidence='needs_fields',type(e).__name__+':'+str(e)[:200],{}
  with connect() as c:
   attempt=row['repair_attempts'] or 0
   if phase=='needs_fields':
    phase='repair_pending';attempt+=1
   due=time.time()+repair_delay(attempt) if phase=='repair_pending' else 0
   c.execute('UPDATE items SET state=?,reason=?,collect_seconds=?,collection=?,updated=?,repair_attempts=?,repair_due=? WHERE sku=?',(phase,reason,time.time()-begin,json.dumps(evidence),time.time(),attempt,due,row['sku']))
   if row['state']=='repair_pending' or phase=='repair_pending':c.execute('INSERT INTO repair_events VALUES(?,?,?,?,?)',(row['sku'],time.time(),attempt,reason,json.dumps(evidence)))
  print(json.dumps({'step':'collect','sku':row['sku'],'state':phase,'seconds':round(time.time()-begin,2),**status()}),flush=True)
  await asyncio.sleep(.5)

async def consume(db):
 while not paused():
  row=take('ready','evaluating')
  if not row:
   with connect() as c:pending=c.execute("SELECT count(*) FROM items WHERE state IN ('pending','collecting','repair_pending','repairing')").fetchone()[0]
   if not pending:return
   await asyncio.sleep(1);continue
  begin=time.time()
  try:
   report=await evaluate(db,OWNER,row['sku'],row['seller']);phase=report['state'];reason=report.get('reason') or report.get('result',{}).get('reason') or ''
  except Exception as e:report={};phase='error';reason=type(e).__name__+':'+str(e)[:200]
  with connect() as c:c.execute('UPDATE items SET state=?,reason=?,report=?,eval_seconds=?,updated=? WHERE sku=?',(phase,str(reason),json.dumps(report),time.time()-begin,time.time(),row['sku']))
  print(json.dumps({'step':'evaluate','sku':row['sku'],'state':phase,'seconds':round(time.time()-begin,2),**status()}),flush=True)

async def main():
 RUN.mkdir(parents=True,exist_ok=True)
 with (RUN/'runner.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);(RUN/'pid').write_text(str(os.getpid()))
  for sig in (signal.SIGINT,signal.SIGTERM):asyncio.get_running_loop().add_signal_handler(sig,halt)
  with connect() as c:
   repair_schema(c)
   cols={r[1] for r in c.execute('PRAGMA table_info(items)')}
   for k,t in [('collect_seconds','REAL'),('eval_seconds','REAL'),('collection','TEXT')]:
    if k not in cols:c.execute(f'ALTER TABLE items ADD COLUMN {k} {t}')
   c.execute("UPDATE items SET state='pending' WHERE state IN ('running','collecting')")
   c.execute("UPDATE items SET state='repair_pending' WHERE state IN ('needs_fields','repairing','error')")
   c.execute("UPDATE items SET state='ready' WHERE state='evaluating'")
  print(json.dumps(status()),flush=True)
  await asyncio.gather(produce(Database()),consume(Database()))
  print(json.dumps({'stopped':paused(),**status()}),flush=True)

def halt():
 global stop
 stop=True

if __name__=='__main__':raise SystemExit('Legacy browser runner retired. Use pipeline_campaigns and python -m flowhub.pipeline_modules status.')
