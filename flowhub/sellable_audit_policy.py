"""Exact owned inventory cards and sales-aware removal; isolated from sourcing jobs."""
import asyncio,collections,json,os,time
from pathlib import Path
from .source_library import fingerprint
from .source_acquisition import page_data
PREFIX='owned-audit:'

def schema(db):
 with db.connect() as c:
  c.execute('CREATE TABLE IF NOT EXISTS sellable_audit_cards(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))')
  c.execute('CREATE TABLE IF NOT EXISTS sellable_audit_events(id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,actor TEXT,action TEXT,note TEXT,revision TEXT,at REAL)')

def sales_index(orders):
 index=collections.Counter();seen=set()
 for order in orders:
  posting=str(order.get('posting_number') or '');shop=str(order.get('shop_id') or '')
  if not posting or not shop or not isinstance(order.get('products'),list):raise ValueError('incomplete_order_identity')
  if (shop,posting) in seen:continue
  seen.add((shop,posting))
  if str(order.get('status')).lower() in ('cancelled','canceled'):continue
  for p in order['products']:
   quantity=p.get('quantity')
   if not isinstance(quantity,(int,float)) or quantity<0:raise ValueError('invalid_order_quantity')
   if not p.get('offer_id') and not p.get('sku'):raise ValueError('incomplete_order_product')
   for kind in ('offer_id','sku'):
    if p.get(kind):index[(shop,kind,str(p[kind]))]+=quantity
 return index

def historical_sales(row,index):
 return max(index.get((str(row['shop_id']),kind,str(row[kind])),0) for kind in ('offer_id','sku'))

def policy(verdict,sales,complete,explicit=False):
 if explicit:return 'queued'
 if verdict!='mismatch':return 'needs_review'
 if not complete or sales is None or sales>0:return 'needs_review'
 return 'queued'

class NativeAPI:
 def __init__(self,token,db=None,owner=None):self.token=token;self.db=db;self.owner=owner
 async def erp(self,method,path,body=None,params=None):
  payload=json.dumps({'method':method,'path':path,'query':params,'body':body,'execute':method=='POST'}).encode()
  attempts=3 if method=='GET' else 1
  last=None
  for attempt in range(attempts):
   p=await asyncio.create_subprocess_exec('node',str(Path(__file__).resolve().parents[1]/'bridges/sellable-audit.mjs'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=os.environ|{'MAOZI_ACCESS_TOKEN':self.token})
   try:
    out,_=await asyncio.wait_for(p.communicate(payload),100)
    result=json.loads(out)
    if result.get('ok'):return result['data']
    error=result.get('error') or {}
    last=RuntimeError(json.dumps(error,ensure_ascii=False))
    retryable=str(error.get('code') or '').upper() in {'UND_ERR_CONNECT_TIMEOUT','UND_ERR_HEADERS_TIMEOUT','ETIMEDOUT','ECONNRESET','ECONNREFUSED'} or 'timeout' in str(error).lower()
    if not retryable or attempt+1>=attempts:raise last
   except (asyncio.TimeoutError, json.JSONDecodeError) as e:
    last=e
    if attempt+1>=attempts:raise
   finally:
    if p.returncode is None:p.kill();await p.wait()
   await asyncio.sleep(2**attempt)
  raise last or RuntimeError('erp_request_failed')
 async def archived_readback(self,row):
  if self.db is None or not self.owner:return False
  from . import official_api
  with self.db.connect() as c:
   stores=[dict(r) for r in c.execute('SELECT config,secret FROM stores WHERE owner=?',(self.owner,))]
  stores=[s for s in stores if str(json.loads(s['config']).get('shop_id'))==str(row['shop_id'])]
  if len(stores)!=1:raise ValueError('official_shop_not_unique')
  keys=self.db.open(stores[0]['secret'])
  async with official_api.client(self.db,keys) as api:
   response=await api.post('/v3/product/info/list',json={'product_id':[int(row['product_id'])]})
   response.raise_for_status();items=response.json().get('items',[])
  matches=[p for p in items if str(p.get('id'))==str(row['product_id']) and str(p.get('offer_id'))==str(row['offer_id']) and str(p.get('sku'))==str(row['sku'])]
  if len(matches)!=1:raise ValueError('official_product_identity_mismatch')
  p=matches[0];stocks=p.get('stocks') or {};entries=stocks.get('stocks') or []
  return p.get('is_archived') is True and stocks.get('has_stock') is False and bool(entries) and all(x.get('present')==0 and x.get('reserved')==0 for x in entries)
 async def rows(self,path,params):
  result=[];seen=set()
  for page in range(1,2001):
   data=await self.erp('GET',path,params=params|{'page':page,'page_size':1000 if 'order' in path else 100})
   rows,last=page_data(data,page,1000 if 'order' in path else 100)
   signature=fingerprint(rows)
   if rows and signature in seen:raise ValueError('repeated_page')
   seen.add(signature);result.extend(rows)
   if not rows or last is not None and page>=last:return result
   if isinstance(data,dict) and isinstance(data.get('total'),int) and len(result)>=data['total']:return result
  raise ValueError('truncated_lookup')


def put(db,owner,row,identity,sales=None,complete=False,explicit=False):
 sku=str(row['sku']);seller=PREFIX+str(row['shop_id']);key=(owner,sku,seller)
 with db.connect() as c:
  old=c.execute('SELECT body FROM sellable_audit_cards WHERE owner=? AND sku=? AND seller=?',key).fetchone()
  if old:return False
  conflict=contradictory_brand_evidence(identity)
  state='needs_review' if conflict and not explicit else policy(identity['verdict'],sales,complete,explicit)
  body={'product':row,'identity':identity,'sales':sales,'sales_complete':complete,'sales_checked_at':time.time(),'explicit_delist':explicit,'evidence_conflict':conflict,'receipt':{},'created_at':time.time(),'note':'用户授权：不同款且历史无销量直接下架；有销量待审核'}
  c.execute('INSERT INTO sellable_audit_cards VALUES(?,?,?,?,?,?)',(*key,state,json.dumps(body),time.time()))
 return True

def save(db,key,state,body):
 with db.connect() as c:c.execute('UPDATE sellable_audit_cards SET state=?,body=?,updated=? WHERE owner=? AND sku=? AND seller=?',(state,json.dumps(body),time.time(),*key))

def cards(c,owner):
 if not c.execute("SELECT 1 FROM sqlite_master WHERE name='sellable_audit_cards'").fetchone():return []
 result=[]
 for row in c.execute('SELECT * FROM sellable_audit_cards WHERE owner=?',(owner,)):
  b=json.loads(row['body']);p=b['product'];identity=b['identity'];cb=identity['comparison'];d=cb.get('decision') or {};q=d.get('qwen_review') or {}
  candidate=next((x for x in cb.get('search_and_rank',{}).get('candidates',[]) if str(x['candidate']['offer_id'])==str(d.get('selected_offer_id'))),{});supplier=candidate.get('candidate') or {}
  pending=row['state'] in ('needs_review','blocked');active=row['state'] in ('queued','waiting','running');sales=b.get('sales')
  sales_note='历史订单尚未完整核实，保留待审核' if not b.get('sales_complete') else f'本店历史未取消订单 {sales} 件；'+('有过销量，保留待审核' if sales else '历史无销量，自动下架')
  if b.get('evidence_conflict'):sales_note+='；模型所述品牌证据不足，保留人工核对'
  label={'needs_review':'不同款结论 · 待人工审核','queued':'不同款 · 待下架','waiting':'下架回查中','delisted':'已下架并核验','retained':'人工确认保留','blocked':'下架受阻 · 待审核'}.get(row['state'],row['state'])
  if identity['verdict']=='uncertain' and pending:
   label='识别不确定 · 待人工审核';sales_note='模型尚不能确认是否同款，保留在售等待人工核对'
  can_retain=pending and not b.get('explicit_delist') and (not b.get('receipt') or b['receipt'].get('rolled_back_verified'))
  actions=(['approve'] if can_retain else [])+(['reject','unlist'] if pending else [])
  result.append({'sku':row['sku'],'seller':row['seller'],'revision':fingerprint(dict(row)),
   'pipeline_state':'needs_review' if pending or row['state']=='blocked' else 'delisted' if row['state']=='delisted' else 'selling' if row['state']=='retained' else 'delisting',
   'review_scope':'same_product_only','category':'same_product','category_label':label,'identity_verdict':identity['verdict'],
   'title':p['name'],'image':p['primary_image'],'url':f"https://www.ozon.ru/product/{row['sku']}/",'target_store_name':p['shop_name'],
   'supplier_title':supplier.get('title'),'supplier_image':supplier.get('image_url'),'supplier_url':supplier.get('offer_url'),'supplier_id':supplier.get('offer_id'),
   'dino_score':candidate.get('dinov2_similarity'),'qwen':q,'reason':sales_note+'。'+str(q.get('reason') or d.get('reason') or '')+'。'+str(b.get('error') or ''),
   'observed_at':identity.get('reviewed_at'),'can_approve':can_retain,'can_reject':pending,'can_unlist':pending,'can_list':False,'can_repair':False,'allowed_actions':actions,
   'listing_state':'waiting' if active else 'unlisted' if row['state']=='delisted' else row['state'],'listing_action':'unlist' if active or row['state']=='delisted' else None,
   'approval_block':None if pending else '等待核验结果','historical_sales':sales,'listing_error':b.get('error'),'missing_fields':[],'profit_rate':None,'purchase':supplier.get('price_cny'),'listing_targets':[b['receipt']]})
 return result

def decide(db,owner,actor,sku,seller,action,note,expected,**kwargs):
 if not note or len(note)>1000:raise ValueError('请填写审核理由')
 with db.connect() as c:
  c.execute('BEGIN IMMEDIATE')
  prior=c.execute('SELECT 1 FROM sellable_audit_events WHERE owner=? AND sku=? AND seller=? AND revision=? AND action=?',(owner,sku,seller,expected,action)).fetchone()
  if prior:return {'action':action,'replayed':True}
  row=c.execute('SELECT * FROM sellable_audit_cards WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
  if not row or fingerprint(dict(row))!=expected or row['state'] not in ('needs_review','blocked'):raise ValueError('审核状态已变化，请刷新')
  if action not in ('approve','reject','unlist'):raise ValueError('当前审核不支持此操作')
  b=json.loads(row['body'])
  if action=='approve' and (b.get('explicit_delist') or (b.get('receipt') and not b['receipt'].get('rolled_back_verified'))):raise ValueError('明确下架或已开始下架的商品不能直接保留')
  b['human']={'actor':actor,'action':action,'note':note,'at':time.time()}
  if action!='approve' and b.get('receipt',{}).get('rolled_back_verified'):
   b.setdefault('past_receipts',[]).append(b['receipt']);b['receipt']={}
  state='retained' if action=='approve' else 'queued'
  c.execute('UPDATE sellable_audit_cards SET state=?,body=?,updated=? WHERE owner=? AND sku=? AND seller=?',(state,json.dumps(b),time.time(),owner,sku,seller))
  c.execute('INSERT INTO sellable_audit_events(owner,sku,seller,actor,action,note,revision,at) VALUES(?,?,?,?,?,?,?,?)',(owner,sku,seller,actor,action,note,expected,time.time()))
  return {'state':state,'action':action}

async def delist_step(api,row,receipt,persist):
 target={'shop_id':str(row['shop_id']),'offer_id':str(row['offer_id'])}
 rows=await api.rows('/api.product.online/lists',target|{'archived_type':'all'})
 matches=[r for r in rows if str(r.get('shop_id'))==target['shop_id'] and str(r.get('offer_id'))==target['offer_id']]
 if not matches and receipt.get('archive_dispatched_at') and hasattr(api,'archived_readback'):
  if await api.archived_readback(row):
   receipt.update(archived_verified_at=time.time(),zero_stock_verified_at=time.time(),phase='verified',verification_source='ozon_official');return 'delisted'
  return 'waiting'
 if len(matches)!=1:raise ValueError('exact_own_product_not_unique')
 current=matches[0]
 if any(str(current.get(k))!=str(row.get(k)) for k in ('sku','product_id')):raise ValueError('own_product_identity_changed')
 if current.get('name')!=row.get('name') or current.get('primary_image')!=row.get('primary_image'):raise ValueError('comparison_input_changed')
 # ERP row IDs are replaced on sync; platform identity above remains authoritative.
 current_id=int(current['id'])
 receipt['current_erp_id']=current_id
 data=await api.erp('GET','/api.product.online/get_stock',params={'id':current_id})
 stocks=data if isinstance(data,list) else data.get('data',[])
 if not isinstance(stocks,list):raise ValueError('stock_schema')
 for s in stocks:
  if str(s.get('offer_id'))!=target['offer_id'] or str(s.get('product_id'))!=str(row['product_id']):raise ValueError('stock_identity_mismatch')
 zero=current.get('stock') in (0,'0') and bool(stocks) and all(type(s.get('present')) is int and s['present']==0 for s in stocks)
 if not stocks:zero=current.get('stock') in (0,'0')
 if not zero:
  shops=await api.rows('/api.shop/lists',{'scene':'erp'});shop=next((s for s in shops if str(s['id'])==target['shop_id']),None)
  ids={str(s['warehouse_id']) for s in stocks if s.get('warehouse_id')};ids.update(str(s['warehouse_id']) for s in (shop or {}).get('warehouse',[]) if s.get('warehouse_id'))
  if not ids:raise ValueError('all_warehouses_unknown')
  receipt.setdefault('original_stocks',stocks);receipt.setdefault('original_online',current)
  receipt.update(phase='zero_stock_dispatched',warehouses=sorted(ids));persist()
  await api.erp('POST','/api.product.online/batch_update_stock',body={'shop_id':int(row['shop_id']),'products':[{'id':current_id,'offer_id':row['offer_id'],'warehouses':[{'warehouse_id':int(w),'stock':0} for w in sorted(ids)]}]})
  return 'waiting'
 receipt['zero_stock_verified_at']=time.time()
 archived=current.get('archived_type') in ('manual','archived','archive','deleted') or current.get('online_status') in ('archived','deleted','disabled')
 if archived:receipt.update(archived_verified_at=time.time(),phase='verified');return 'delisted'
 if not receipt.get('archive_dispatched_at'):
  receipt.update(phase='archive_dispatched',archive_dispatched_at=time.time());persist()
  await api.erp('POST','/api.product.online/archive',body={'ids':[current_id]})
  await api.erp('POST','/api.product.online/sync_shop',body={'ids':[int(row['shop_id'])],'type':'all'})
 return 'waiting'


def contradictory_brand_evidence(identity):
 """A missing/unknown brand cannot prove the explicit conflict needed to delist."""
 q=(identity.get('comparison',{}).get('decision',{}).get('qwen_review') or {})
 reason=str(q.get('reason') or '').lower()
 return bool(q.get('brand_or_model_conflict')) and any(s in reason for s in ('无品牌','没有品牌','未标注品牌','未明确品牌','未显示品牌','品牌不明','看不清','no brand','unbranded','brand unknown'))
