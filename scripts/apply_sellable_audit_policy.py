"""Persist and execute the owner's sales-aware rule for this bounded audit batch."""
import asyncio,collections,fcntl,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.control import load_runtime_env
from flowhub.sellable_audit_policy import NativeAPI,schema,sales_index,historical_sales,put,save,delist_step
from flowhub.source_delists import read_delists
from flowhub.remote_reviews import once
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'reports/sellable-identity-20260916'

def write(name,value):
 p=OUT/name;tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(p)

async def main():
 lock=(OUT/'policy.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 load_runtime_env();db=Database();schema(db);owner=os.environ['FLOWHUB_REVIEW_OWNER']
 with db.connect() as c:stores=[dict(r) for r in c.execute('SELECT * FROM stores WHERE owner=?',(owner,))]
 allowed={str(json.loads(r['config'])['shop_id']):r for r in stores}
 api=NativeAPI(db.open(stores[0]['secret'])['erp_token'],db=db,owner=owner);index=None;index_at=0;blocks=None;block_at=0;last_sync=0
 handled=set();inventory={};loaded=set();state={'started_at':time.time(),'phase':'running','rule':'不同款且历史未取消订单为零直接下架；有销量或无法核实保留待审核'}
 while True:
  try:
   for f in sorted(OUT.glob('page-*.json')):
    if f.name in loaded:continue
    data=json.loads(f.read_text());rows=data if isinstance(data,list) else data['data']
    for p in rows:
     if str(p['shop_id']) in allowed:inventory[str(p['shop_id'])+':'+str(p['id'])]=p
    loaded.add(f.name)
   if time.time()-index_at>300:
    index_at=time.time()
    try:
     orders=await api.rows('/api.order.ozon/lists',{})
     index=sales_index(orders)
     write('sales-evidence.json',{'at':index_at,'complete':True,'order_count':len(orders),'index':[{'shop':k[0],'kind':k[1],'value':k[2],'quantity':v} for k,v in index.items()]})
     state.update(historical_orders=len(orders),sales_checked_at=index_at)
    except Exception as e:
     index=None;state['sales_error']=str(e)[:250]
   if blocks is None or time.time()-block_at>300:
    blocks=await read_delists();block_at=time.time()
   file=OUT/'results.jsonl'
   rows=[json.loads(l) for l in file.read_text().splitlines()] if file.exists() else []
   # Last attempt wins; technical failures never authorize removal.
   latest={r['key']:r for r in rows}
   for key,r in latest.items():
    if key in handled or r['verdict']!='mismatch' or key not in inventory:continue
    p=inventory[key];identity=json.loads((OUT/f"evidence-{key.replace(':','-')}.json").read_text())
    query=identity['comparison']['search_and_rank']['query']
    if str(query['product_id'])!=str(p['sku']) or query['title']!=p['name'] or query['image_url']!=p['primary_image']:raise ValueError('audit_comparison_binding_mismatch')
    explicit=str(p['sku']) in blocks['skus'] or [str(p['shop_id']),str(p['offer_id'])] in blocks['offers'] or ['*',str(p['offer_id'])] in blocks['offers']
    put(db,owner,p,identity,historical_sales(p,index) if index is not None else None,index is not None,explicit);handled.add(key)
   with db.connect() as c:pending=[dict(r) for r in c.execute("SELECT * FROM sellable_audit_cards WHERE owner=? AND state IN ('queued','waiting') ORDER BY updated LIMIT 10",(owner,))]
   for item in pending:
    key=(owner,item['sku'],item['seller']);b=json.loads(item['body']);p=b['product'];receipt=b['receipt']
    if time.time()<b.get('next_attempt',0):continue
    try:
     # A fresh full own-order check precedes the first stock mutation.
     if not receipt.get('zero_stock_verified_at'):
      orders=await api.rows('/api.order.ozon/lists',{});index=sales_index(orders);index_at=time.time()
      b.update(sales=historical_sales(p,index),sales_complete=True,sales_checked_at=index_at)
      blocks=await read_delists();block_at=time.time()
      explicit=str(p['sku']) in blocks['skus'] or [str(p['shop_id']),str(p['offer_id'])] in blocks['offers'] or ['*',str(p['offer_id'])] in blocks['offers']
      b['explicit_delist']=explicit
      if b['sales']>0 and not explicit and (b.get('human') or {}).get('action') not in ('reject','unlist'):
       save(db,key,'needs_review',b);continue
     next_state=await delist_step(api,p,receipt,lambda:save(db,key,'waiting',b))
     b.pop('error',None);b['next_attempt']=time.time()+30
     if next_state=='delisted':
      with db.connect() as c:c.execute('INSERT OR IGNORE INTO blocks VALUES(?,?,?)',(owner,str(p['sku']),'sellable_audit_mismatch'))
     save(db,key,next_state,b)
    except Exception as e:
     b['error']=str(e)[:300];b['next_attempt']=time.time()+60;b['failures']=b.get('failures',0)+1
     save(db,key,'blocked' if b['failures']>=6 or isinstance(e,ValueError) else 'waiting',b)
   if time.time()-last_sync>60:
    await once(db,refresh=True);last_sync=time.time()
   with db.connect() as c:state['counts']=dict(c.execute('SELECT state,count(*) FROM sellable_audit_cards WHERE owner=? GROUP BY state',(owner,)).fetchall())
   state.update(updated_at=time.time(),processed_mismatches=len(handled));state.pop('error',None);write('policy-status.json',state)
   print(json.dumps(state,ensure_ascii=False),flush=True)
   audit=json.loads((OUT/'status.json').read_text())
   if audit.get('phase') in ('complete','incomplete') and not pending:
    state['phase']='initial_pass_complete';write('policy-status.json',state)
    # Keep processing explicit website decisions for this same bounded batch.
   await asyncio.sleep(30)
  except Exception as e:
   state.update(error=type(e).__name__+': '+str(e)[:300],updated_at=time.time());write('policy-status.json',state);print(json.dumps(state,ensure_ascii=False),flush=True);await asyncio.sleep(60)

if __name__=='__main__':asyncio.run(main())
