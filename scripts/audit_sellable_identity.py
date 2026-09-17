"""Read-only sellable inventory audit using FlowHub compareBot; no listing mutations."""
import asyncio, collections, csv, fcntl, html, json, os, sys, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.control import load_runtime_env
from flowhub.db import Database
from flowhub.source_acquisition import erp_request,page_data,archived_state
from flowhub.comparebot import screen
from flowhub.identity_review import identity_result
from flowhub.comparebot_process import close_workers
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/sellable-identity-20260916'

def save(path,value):
 tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(path)

def key(row):return str(row['shop_id'])+':'+str(row['id'])
def sellable(row):
 return row.get('online_status')=='selling' and float(row.get('stock') or 0)>0 and not archived_state(row)

def render(state,items,results):
 results={k:r for k,r in results.items() if k in items}
 counts=collections.Counter(r['verdict'] for r in results.values())
 state.update(updated_at=time.time(),sellable=len(items),reviewed=len(results),counts=dict(counts))
 save(OUT/'status.json',state)
 labels={'match':'同款','mismatch':'不同款','uncertain':'待人工核对','no_candidates':'未搜到货源','error':'技术异常待重试'}
 fields=['店铺','Ozon SKU','货号','结论','DINO','1688链接','Ozon链接','原因']
 with (OUT/'results.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.writer(f);w.writerow(fields)
  for k,r in results.items():
   p=items[k];w.writerow([p['shop_name'],p['sku'],p['offer_id'],labels[r['verdict']],r.get('score'),r.get('supplier_url'),f"https://www.ozon.ru/product/{p['sku']}/",r.get('reason','')])
 esc=lambda x:html.escape(str(x or ''),quote=True)
 cards=[]
 for k,r in results.items():
  p=items[k];cards.append(f"<article><h3>{esc(p['shop_name'])} · {esc(p['sku'])} · {labels[r['verdict']]}</h3><p>{esc(p['name'])}</p><div><a href='https://www.ozon.ru/product/{esc(p['sku'])}/'><img src='{esc(p['primary_image'])}'></a><a href='{esc(r.get('supplier_url'))}'><img src='{esc(r.get('supplier_image'))}'></a></div><p>DINO: {esc(r.get('score'))} {esc(r.get('reason'))}</p></article>")
 (OUT/'review.html').write_text('<!doctype html><meta charset="utf-8"><title>在售商品同款筛查</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;background:#f5f5f5}article{background:white;padding:20px;margin:16px 0}img{width:240px;height:220px;object-fit:contain}h1{font-size:28px}</style><h1>FlowHub 在售商品 × 1688 同款筛查</h1><p>仅筛查；未修改库存或上下架状态。未找到候选和技术异常不等于不同款。</p><p>'+esc(json.dumps(state,ensure_ascii=False))+'</p>'+''.join(cards))

async def main():
 OUT.mkdir(parents=True,exist_ok=True)
 lock=(OUT/'run.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 load_runtime_env();os.environ.setdefault('FLOWHUB_COMPAREBOT_WARM','1');os.environ.setdefault('HF_HUB_OFFLINE','1');os.environ.setdefault('TOKENIZERS_PARALLELISM','false')
 db=Database();owner=os.environ['FLOWHUB_REVIEW_OWNER']
 with db.connect() as c:
  stores=[dict(r) for r in c.execute('select * from stores where owner=?',(owner,))]
  wf=c.execute('select secrets from workflows where owner=?',(owner,)).fetchone()
 allowed={str(json.loads(r['config'])['shop_id']):r['name'] for r in stores}
 token=db.open(stores[0]['secret'])['erp_token'];raw=db.open(wf[0])['flowb-matcher'];keys=json.loads(raw) if raw.startswith('{') else {}
 api_key=keys.get('dashscope_api_key','')
 if not api_key:raise RuntimeError('Qwen key missing; full audit not started')
 state={'started_at':time.time(),'phase':'inventory_and_screening','shops':allowed,'inventory_complete':False,'page':0,'inventory_rows':0}
 items={};results={};queue=asyncio.Queue();seen=set();signatures=set();finished=asyncio.Event()
 if (OUT/'results.jsonl').exists():
  for line in (OUT/'results.jsonl').read_text().splitlines():
   r=json.loads(line)
   if r['verdict']!='error':results[r['key']]=r
 async def inventory():
  try:
   for page in range(1,2001):
    file=OUT/f'page-{page:04}.json'
    if file.exists():data=json.loads(file.read_text())
    else:
     for attempt in range(6):
      try:
       data=await erp_request('/api.product.online/lists',{'page':page,'page_size':100,'archived_type':'all'},token);break
      except Exception as e:
       state['inventory_retry']={'page':page,'attempt':attempt+1,'error':type(e).__name__,'diagnostic':getattr(e,'diagnostic',None)}
       render(state,items,results)
       if attempt==5:raise
       await asyncio.sleep(60)
     save(file,data)
    rows,last=page_data(data,page,100)
    signature=tuple(sorted(key(r) for r in rows))
    if rows and signature in signatures:raise ValueError('repeated_inventory_page')
    signatures.add(signature)
    state.update(page=page,reported_total=data.get('total') if isinstance(data,dict) else None)
    for r in rows:
     k=key(r)
     if k in seen:continue
     seen.add(k);state['inventory_rows']+=1
     if str(r.get('shop_id')) not in allowed or not sellable(r):continue
     items[k]=r
     if k not in results:queue.put_nowait((k,r))
    render(state,items,results)
    print(json.dumps({'page':page,'read':len(seen),'sellable':len(items),'reviewed':len(results)}),flush=True)
    if not rows or (last is not None and page>=last):
     state['inventory_complete']=True;break
   else:raise ValueError('inventory_page_limit')
  except Exception as e:
   state.update(inventory_error=type(e).__name__+': '+str(e),phase='inventory_blocked')
  finally:
   save(OUT/'manifest.json',list(items.values()));finished.set();render(state,items,results)
 async def review():
  consecutive_errors=0
  while not finished.is_set() or not queue.empty():
   try:k,p=await asyncio.wait_for(queue.get(),2)
   except asyncio.TimeoutError:continue
   began=time.time();r={'key':k,'verdict':'error','reviewed_at':began}
   candidate={'source_key':str(p['sku']),'title':p.get('name'),'image':p.get('primary_image'),'origin':{'specifications':{}}}
   try:
    if not candidate['title'] or not candidate['image']:raise ValueError('current_title_or_image_missing')
    cb=await screen(candidate)
    ranking=cb.get('search_and_rank')
    if ranking and ranking.get('candidates'):cb=await screen(candidate,api_key,ranking=ranking)
    identity=identity_result(cb,candidate);save(OUT/f"evidence-{k.replace(':','-')}.json",identity)
    d=cb.get('decision') or {};q=d.get('qwen_review') or {}
    selected=next((x for x in (ranking or {}).get('candidates',[]) if str(x['candidate']['offer_id'])==str(d.get('selected_offer_id'))),{})
    supplier=selected.get('candidate') or {}
    r.update(verdict=identity['verdict'],score=selected.get('dinov2_similarity'),supplier_url=supplier.get('offer_url'),supplier_image=supplier.get('image_url'),reason=q.get('reason') or d.get('reason'),qwen=q)
    consecutive_errors=0
   except Exception as e:
    r['reason']=type(e).__name__+': '+str(e)[:250];consecutive_errors+=1
   r['seconds']=round(time.time()-began,2);results[k]=r
   with (OUT/'results.jsonl').open('a') as f:f.write(json.dumps(r,ensure_ascii=False)+'\n')
   render(state,items,results);print(json.dumps({'reviewed':len(results),'verdict':r['verdict'],'sku':p['sku'],'seconds':r['seconds']}),flush=True);queue.task_done()
   if consecutive_errors>=3:
    state.update(screening_error='Three consecutive compute failures; retained inventory and remaining queue',phase='screening_blocked');render(state,items,results);return
 await asyncio.gather(inventory(),review())
 state['phase']='complete' if state['inventory_complete'] and not state.get('screening_error') and not any(r['verdict']=='error' for r in results.values()) else 'incomplete'
 render(state,items,results);await close_workers()

if __name__=='__main__':asyncio.run(main())
