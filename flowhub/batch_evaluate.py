"""Resumable evaluation-only run over a frozen user-authorized source manifest."""
import asyncio,csv,fcntl,json,signal,sqlite3,time
from pathlib import Path
from .db import Database,ROOT
from .plugin_detail import enrich_plugin_detail
from .plugin_comparebot import evaluate
from .source_library import SourceLibrary

RUN=ROOT/'reports/batch-922-20260912'
OWNER='local-owner'  # Historical helpers only; production uses the authenticated owner.
stop=False

def connection():
 c=sqlite3.connect(RUN/'progress.sqlite3',timeout=30);c.row_factory=sqlite3.Row;return c

def summary():
 with connection() as c:
  rows=[dict(r) for r in c.execute('SELECT * FROM items ORDER BY position')]
 counts={}
 for r in rows:counts[r['state']]=counts.get(r['state'],0)+1
 report={'at':time.time(),'total':len(rows),'counts':counts,'completed':sum(v for k,v in counts.items() if k not in ('pending','running')),'remaining':sum(v for k,v in counts.items() if k in ('pending','running'))}
 (RUN/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 (RUN/'进度.md').write_text('# 922 件候选批量测算\n\n'+f"更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}。已处理 {report['completed']} / 922，剩余 {report['remaining']}。\n\n"+'\n'.join(f'- {k}：{v}' for k,v in counts.items())+'\n\nmatched 为测算通过；rejected 为未通过；needs_review 为需人工核对；needs_data 为资料不足，尚未完成测算；error 为执行错误。只做测算，不自动上架。逐件结果见 results.csv，原始接口证据见 facts，完整测算证据保存在 progress.sqlite3 和 FlowHub 来源库。\n')
 with (RUN/'results.csv').open('w') as f:
  writer=csv.writer(f);writer.writerow(['sku','seller','state','reason','seconds','supplier','purchase_cny','sell_cny','profit_pct'])
  for row in rows:
   r=json.loads(row['report'] or '{}');result=r.get('result',{});profit=result.get('evidence',{}).get('profit',{})
   writer.writerow([row['sku'],row['seller'],row['state'],row['reason'],row['seconds'],result.get('supplier_id'),result.get('purchase'),profit.get('sell_price_cny'),profit.get('assessment',{}).get('erp_profit_rate_pct')])
 return report

async def one(db,row):
 sku=row['sku'];seller=row['seller'];begin=time.time()
 process=await asyncio.create_subprocess_exec('node',str(ROOT/'bridges/plugin-batch-detail.mjs'),sku,seller,str(RUN/'facts'),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
 try:out,err=await asyncio.wait_for(process.communicate(),140)
 except TimeoutError:
  process.kill();await process.wait();raise RuntimeError('detail_deadline')
 if process.returncode:raise RuntimeError('detail_process_failed')
 response=json.loads(out)
 if response['state']!='ready':return 'needs_data',response.get('error'),{},time.time()-begin
 packet=json.loads(Path(response['file']).read_text())
 with db.connect() as c:
  p=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(OWNER,sku,seller)).fetchone()
  if p is None:raise ValueError('source_missing')
  product=enrich_plugin_detail(json.loads(p[0]),packet['sales'],packet['variant'])
 SourceLibrary(db).put(OWNER,product,{'channel':'batch-922-plugin','observed_at':time.time()})
 report=await evaluate(db,OWNER,sku,seller)
 reason=report.get('reason') or report.get('result',{}).get('reason') or report.get('result',{}).get('rejected')
 return report['state'],str(reason) if reason else '',report,time.time()-begin

async def main():
 global stop
 RUN.mkdir(parents=True,exist_ok=True)
 with (RUN/'runner.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  import os
  (RUN/'pid').write_text(str(os.getpid()))
  for sig in (signal.SIGTERM,signal.SIGINT):asyncio.get_running_loop().add_signal_handler(sig,lambda:set_stop())
  manifest=json.loads((RUN/'manifest.json').read_text())
  assert len(manifest)==922 and len({p['sku'] for p in manifest})==922
  with connection() as c:
   c.execute('CREATE TABLE IF NOT EXISTS items(sku TEXT PRIMARY KEY,seller TEXT,position INTEGER,state TEXT,reason TEXT,report TEXT,seconds REAL,attempts INTEGER DEFAULT 0,updated REAL)')
   c.executemany("INSERT OR IGNORE INTO items(sku,seller,position,state,updated) VALUES(?,?,?,'pending',?)",[(p['sku'],p['seller_id'],i,time.time()) for i,p in enumerate(manifest)])
   c.execute("UPDATE items SET state='pending' WHERE state='running'")
  db=Database();print(json.dumps(summary()),flush=True)
  while not stop and not (RUN/'PAUSE').exists():
   with connection() as c:
    row=c.execute("SELECT * FROM items WHERE state='pending' ORDER BY attempts,position LIMIT 1").fetchone()
    if not row:break
    c.execute("UPDATE items SET state='running',attempts=attempts+1,updated=? WHERE sku=?",(time.time(),row['sku']))
   try:state,reason,report,seconds=await one(db,row)
   except Exception as e:state,reason,report,seconds='error',type(e).__name__+':'+str(e)[:200],{},0
   # Retry transient failures once; dispatched template requests are never repeated.
   if state=='error' and row['attempts']<1:state='pending'
   if state=='needs_data' and reason.startswith(('opencli_','sales_http_5','base_http_5')) and row['attempts']<1:state='pending'
   with connection() as c:c.execute('UPDATE items SET state=?,reason=?,report=?,seconds=?,updated=? WHERE sku=?',(state,reason,json.dumps(report),seconds,time.time(),row['sku']))
   print(json.dumps({'sku':row['sku'],'state':state,'reason':reason,'seconds':round(seconds,2),**summary()},ensure_ascii=False),flush=True)
   await asyncio.sleep(1)
  print(json.dumps({'stopped':stop or (RUN/'PAUSE').exists(),**summary()}),flush=True)

def set_stop():
 global stop
 stop=True

if __name__=='__main__':raise SystemExit('Historical fixed batch retired. Use pipeline_campaigns and python -m flowhub.pipeline_modules status.')
