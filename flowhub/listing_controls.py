"""Durable website listing/unlisting intents, separate from same-product decisions."""
import asyncio,json,time,uuid
from .maozi import MaoziPublisher
from .source_delists import read_delists
from .official_api import OfficialDeferred


def schema(db):
 with db.connect() as c:c.execute('''CREATE TABLE IF NOT EXISTS product_listing_controls(owner TEXT,sku TEXT,seller TEXT,action TEXT,state TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))''')


def request(c,owner,sku,seller,action,actor,note):
 if action not in ('list','unlist'):raise ValueError('invalid_listing_action')
 previous=c.execute('SELECT * FROM product_listing_controls WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
 if previous and previous['state'] in ('queued','running','waiting'):
  if previous['action']==action:return {'state':previous['state'],'action':action}
  if not (action=='unlist' and previous['action']=='list' and previous['state']!='running'):
   raise ValueError('原下架操作正在回查，完成后可恢复上架')
 body={'id':str(uuid.uuid4()),'actor':actor,'note':note,'requested_at':time.time(),'previous':{k:previous[k] for k in ('action','state','updated')} if previous else None}
 c.execute('INSERT OR REPLACE INTO product_listing_controls VALUES(?,?,?,?,?,?,?)',(owner,sku,seller,action,'queued',json.dumps(body),time.time()))
 return {'state':'queued','action':action}


def context(db,key):
 with db.connect() as c:
  publication=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',key).fetchone()
  record=json.loads(publication[0]) if publication else None
  route=c.execute('SELECT store_id FROM plugin_routes WHERE owner=? AND sku=? AND seller=?',key).fetchone()
  sid=record['store_id'] if record else route[0] if route else None
  store=c.execute('SELECT * FROM stores WHERE owner=? AND id=?',(key[0],sid)).fetchone()
 if not store:raise ValueError('未找到商品绑定的目标店铺')
 cfg=json.loads(store['config']);api=MaoziPublisher({'store':{'config':cfg,'credentials':db.open(store['secret'])}})
 return api,cfg,record,dict(store)


async def owned_targets(api,cfg,record,sku):
 """Only exact own offer bindings; source seller SKUs are never removal targets."""
 if record:return [{'shop_id':str(cfg['shop_id']),'offer_id':record['offer_id']}]
 logs=await api.rows('/api.product.import_logs/index',{'sku':sku})
 shops=None;targets=[]
 for row in logs:
  if str(row.get('sku'))!=sku:continue
  for item in [row,*(row.get('skus') or [])]:
   offer=str(item.get('offer_id') or '')
   if not offer:continue
   shop=item.get('shop_id')
   if not shop and item.get('shop_name'):
    if shops is None:shops=await api.rows('/api.shop/lists',{'scene':'erp'})
    exact=[s for s in shops if s.get('name')==item['shop_name']]
    if len(exact)!=1:raise ValueError('导入记录的店铺名不能唯一定位')
    shop=exact[0]['id']
   if not shop:raise ValueError('导入记录缺少精确店铺')
   targets.append({'shop_id':str(shop),'offer_id':offer})
 return list({(r['shop_id'],r['offer_id']):r for r in targets}.values())


async def online(api,target):
 rows=await api.rows('/api.product.online/lists',target|{'archived_type':'all'})
 exact=[r for r in rows if str(r.get('shop_id'))==target['shop_id'] and str(r.get('offer_id'))==target['offer_id']]
 if len(exact)>1:raise ValueError('同一店铺货号出现多个商品记录')
 return exact[0] if exact else None


async def remove(db,key,body):
 api,cfg,record,_=context(db,key)
 if record and record.get('backend')=='official':
  from .official_maintenance import remove as official_remove
  return await official_remove(db,key,body,record,cfg,api.keys)
 if 'targets' not in body:body['targets']=await owned_targets(api,cfg,record,key[1])
 if not body['targets']:return 'no_listing',body|{'reason':'未发现该来源商品在本店的发布记录，未对来源卖家操作','targets':[]}
 pending=False
 for target in body['targets']:
  row=await online(api,target)
  if row is None:
   if record and record.get('phase') not in ('failed',):
    pending=True;target['phase']='awaiting_original_import_before_delist'
   else:target['verified_absent']=True
   continue
  data=await api.erp('GET','/api.product.online/get_stock',params={'id':int(row['id'])})
  stocks=data if isinstance(data,list) else data.get('data',[])
  if not isinstance(stocks,list):raise ValueError('库存回查格式不完整')
  for stock in stocks:
   if str(stock.get('offer_id'))!=target['offer_id'] or str(stock.get('product_id'))!=str(row.get('product_id')):raise ValueError('库存与目标商品不一致')
  zero=all(type(s.get('present')) is int and s['present']==0 for s in stocks)
  if not stocks and row.get('stock') not in (0,'0'):zero=False
  if not zero:
   shops=await api.rows('/api.shop/lists',{'scene':'erp'})
   shop=next((s for s in shops if str(s['id'])==target['shop_id']),None)
   ids={str(s['warehouse_id']) for s in stocks if s.get('warehouse_id')}
   ids.update(str(s['warehouse_id']) for s in (shop or {}).get('warehouse',[]) if s.get('warehouse_id'))
   if not ids:raise ValueError('无法确认全部可售仓库')
   await api.erp('POST','/api.product.online/batch_update_stock',body={'shop_id':int(target['shop_id']),'products':[{'id':int(row['id']),'offer_id':target['offer_id'],'warehouses':[{'warehouse_id':int(w),'stock':0} for w in sorted(ids)]}]})
   pending=True;target['phase']='zero_stock_readback';continue
  target['zero_stock_verified']=True
  if str(row.get('archived_type','')).lower() in ('manual','archived','archive','deleted') or row.get('online_status') in ('archived','deleted','disabled'):
   target['archived_verified']=True;continue
  if not target.get('archive_dispatched'):
   target['archive_dispatched']=True;save(db,key,'running',body)
   await api.erp('POST','/api.product.online/archive',body={'ids':[int(row['id'])]})
   await api.erp('POST','/api.product.online/sync_shop',body={'ids':[int(target['shop_id'])],'type':'all'})
  pending=True;target['phase']='archive_readback'
 return ('waiting' if pending else 'unlisted'),body


def save(db,key,state,body):
 with db.connect() as c:
  c.execute('UPDATE product_listing_controls SET state=?,body=?,updated=? WHERE owner=? AND sku=? AND seller=?',(state,json.dumps(body),time.time(),*key))
  if state=='unlisted':c.execute("UPDATE plugin_pipeline SET state='delisted' WHERE owner=? AND sku=? AND seller=?",key)
  if state=='no_listing':c.execute("UPDATE plugin_pipeline SET state='not_listed' WHERE owner=? AND sku=? AND seller=?",key)
  if state=='listed':c.execute("UPDATE plugin_pipeline SET state='selling' WHERE owner=? AND sku=? AND seller=?",key)


async def prepare_listing(db,key,body):
 from .identity_review import comparison,bound,envelope
 from .evaluation_requirements import review_sale_price
 from .plugin_comparebot import candidate
 from .plugin_publication import require_quota
 from . import compat
 api,cfg,record,store=context(db,key)
 if record and record.get('backend')=='official':
  from .official_maintenance import restore as official_restore
  return await official_restore(db,key,body,record,cfg,api.keys)
 blocks=await read_delists()
 if key[1] in blocks['skus']:raise ValueError('商品在飞书明确下架清单中')
 from .official_publication import backend
 new_official=not record and backend(cfg)!='maozi'
 # A new official intent uses the local/official preflight in the publication
 # lane. Do not reintroduce the old import-log dependency while resuming review.
 targets=[] if new_official else await owned_targets(api,cfg,record,key[1])
 if targets:
  if not record or record.get('phase') not in (None,'prepared','ready','favorite_pending'):return await restore(db,key,body,api,cfg,targets,blocks)
  observed=await online(api,targets[0])
  if observed:return await restore(db,key,body,api,cfg,targets,blocks)
 if new_official:
  from .official_api import client,capacity
  if not api.keys.get('client_id') or not api.keys.get('api_key'):raise ValueError('official_credentials_required')
  async with client(db,api.keys) as official:quota=await capacity(official)
 else:quota=await api.erp('POST','/api.shop/sync_single_product_limit',body={'id':int(cfg['shop_id'])})
 body['quota']=quota
 try:require_quota(quota)
 except ValueError:
  if new_official:
   from .pipeline_modules.store_capacity import observe
   observe(db,key[0],store['id'],quota)
   return 'waiting',body|{'next_attempt_at':time.time()+900,'error':'target_quota_unavailable'}
  from .listing_fallback import choose
  if record:raise ValueError('已有发布记录，需回查原店结果，不能自动换店重复发布')
  chosen=await choose(db,key,body,api,store,quota)
  if chosen is None:return 'waiting',body
  api,cfg,record,store=chosen
 body.pop('next_attempt_at',None)
 body.pop('error',None)
 with db.connect() as c:
  p=json.loads(c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0])
  r=json.loads(c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0])
  rules=json.loads(c.execute('SELECT rules FROM workflows WHERE owner=?',(key[0],)).fetchone()[0])
 physical=p.get('plugin_detail') or {}
 if not physical.get('weight_g') or len(physical.get('dimensions_mm') or [])!=3:
  from .pipeline_modules.repair import PriceRepairModule
  repair=await PriceRepairModule().run(db,*key,full_dossier=True)
  body['field_repair']=repair.get('reason')
  with db.connect() as c:p=json.loads(c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0])
  physical=p.get('plugin_detail') or {}
  if not physical.get('weight_g') or len(physical.get('dimensions_mm') or [])!=3:raise ValueError('缺少实际包装重量或尺寸')
 cb=(r.get('identity_review') or {}).get('comparison') or comparison(r)
 if not bound(cb,envelope(p)):raise ValueError('同款对比资料已变化，需要重新核对')
 if (r.get('identity_review') or {}).get('verdict')!='match':raise ValueError('需先在网站确认同款，才能上架')
 quote=review_sale_price(p)
 if not quote:raise ValueError('缺少上架售价')
 # User is choosing an asking price now; its historical market timestamp remains
 # nested in reference_quote and is never represented as a new market observation.
 p['proposed_sale_price']={**quote,'observed_at':time.time(),'source':'website-listing-price-intent','reference_quote':quote}
 d=p.get('plugin_detail') or {}
 if d.get('monthly_sales'):d['monthly_sales']={**d['monthly_sales'],'average_price_rub':None}
 env=candidate(p)
 env['origin']['website_listing_authorization']={'id':body['id'],'sku':key[1],'at':body['requested_at'],'same_product_confirmed':True}
 cb=json.loads(json.dumps(cb))
 if (r.get('identity_review') or {}).get('automatic_review'):
  cb['decision']['outcome']='approved'
 if (r.get('identity_review') or {}).get('human_review'):
  cb['decision']['outcome']='approved'
  cb['decision']['human_review']=r['identity_review']['human_review']
 selected=next(x for x in cb['search_and_rank']['candidates'] if str(x['candidate']['offer_id'])==str(cb['decision']['selected_offer_id']))
 offer=selected['candidate']
 source={'selected_offer_id':str(offer['offer_id']),'selected_offer_url':offer['offer_url'],'selected_title':offer['title'],'selected_cost_cny':float(offer['price_cny']),'selected_image_url':offer['image_url'],'selected_offer_image':{'available':True,'score':selected['dinov2_similarity']},'comparebot':cb,'evaluation_only':False}
 result=await compat.invoke('match',{'candidate':env,'rules':rules},api.keys['erp_token'],source=source)
 if result.get('rejected') or result.get('manual_review'):raise ValueError('上架数据检查未通过：'+str(result.get('reason') or '物流或类目不可用'))
 new={'sku':key[1],'seller':key[2],'state':'matched','candidate':env,'result':result,'started_at':time.time(),'finished_at':time.time(),'identity_review':r['identity_review'],'publication_blockers':env['origin']['publication_blockers'],'price_basis':env['origin']['price_evidence'],'website_listing_authorization':{'id':body['id'],'actor':body['actor'],'at':body['requested_at'],'price_policy':'available_asking_price','same_product_only':True}}
 expiry=time.time()+86400
 with db.connect() as c:
  c.execute('UPDATE plugin_reviews SET state=?,body=?,updated=? WHERE owner=? AND sku=? AND seller=?',('matched',json.dumps(new),time.time(),*key))
  c.execute('UPDATE plugin_routes SET expires=?,run_id=? WHERE owner=? AND sku=? AND seller=?',(expiry,body['id'],*key))
  c.execute('INSERT OR REPLACE INTO plugin_publication_permissions VALUES(?,?,?,?,?)',(*key,expiry,'用户要求网站同款上架'))
  q=json.loads(c.execute('SELECT body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0]);q.update(listing_control_id=body['id'],same_product_only=True);q.pop('repair_retry',None);q.pop('error',None);q.pop('reason',None)
  c.execute("UPDATE plugin_pipeline SET state='publishing',body=?,due=0,attempts=0 WHERE owner=? AND sku=? AND seller=?",(json.dumps(q),*key))
 return 'waiting',body|{'phase':'publication_pipeline'}


async def tick(db, *, target=None):
 if target is not None and (len(target)!=3 or not all(isinstance(x,str) and x for x in target)):
  raise ValueError("exact_listing_identity_required")
 scope=" AND owner=? AND sku=? AND seller=?" if target else ""
 schema(db)
 with db.connect() as c:
  c.execute('BEGIN IMMEDIATE')
  c.execute("UPDATE product_listing_controls SET state='waiting' WHERE state='running' AND updated<? AND NOT EXISTS(SELECT 1 FROM plugin_pipeline_leases l WHERE l.owner=product_listing_controls.owner AND l.sku=product_listing_controls.sku AND l.seller=product_listing_controls.seller AND l.expires>?)",(time.time()-360,time.time()))
  row=c.execute("SELECT * FROM product_listing_controls WHERE (state='queued' OR (state='waiting' AND updated<?)) AND COALESCE(json_extract(body,'$.next_attempt_at'),0)<=CAST(strftime('%s','now') AS REAL) AND NOT EXISTS(SELECT 1 FROM plugin_pipeline_leases l WHERE l.owner=product_listing_controls.owner AND l.sku=product_listing_controls.sku AND l.seller=product_listing_controls.seller AND l.expires>strftime('%s','now')) "+scope+" ORDER BY CASE state WHEN 'queued' THEN 0 ELSE 1 END,CASE action WHEN 'unlist' THEN 0 ELSE 1 END,updated LIMIT 1",(time.time()-5,*(target or ()))).fetchone()
  if not row:return False
  key=(row['owner'],row['sku'],row['seller']);body=json.loads(row['body'])
  if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(*key,time.time())).fetchone():return False
  token='listing-control-'+body['id']
  c.execute('INSERT OR REPLACE INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(*key,token,time.time()+360))
  c.execute("UPDATE product_listing_controls SET state='running',updated=? WHERE owner=? AND sku=? AND seller=?",(time.time(),*key))
 try:
  if row['action']=='unlist':state,body=await remove(db,key,body)
  elif body.get('phase')=='publication_pipeline':
   with db.connect() as c:q=c.execute('SELECT state,body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()
   if q['state']=='selling':state='listed'
   elif q['state'] in ('needs_review','needs_fields','rejected','awaiting_dependency','quarantined'):state='blocked';body['error']=json.loads(q['body']).get('error') or json.loads(q['body']).get('reason') or q['state']
   else:state='waiting'
  else:state,body=await prepare_listing(db,key,body)
 except OfficialDeferred as e:
  state='waiting';body['next_attempt_at']=e.until;body['dependency_wait']=e.reason
 except asyncio.CancelledError:
  save(db,key,'waiting',body)
  with db.connect() as c:c.execute('DELETE FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token))
  raise
 except Exception as e:state='blocked';body['error']=(str(e) or type(e).__name__)[:220]
 if state=='waiting' and body.get('phase') not in ('publication_pipeline','capacity_wait') and time.time()-max(body['requested_at'],body.get('workflow_repaired_at',0))>1800:state='blocked';body['error']='上下架回查尚未完成，请查看原记录后重试'
 save(db,key,state,body)
 with db.connect() as c:c.execute('DELETE FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token))
 return True


async def run(db):
 schema(db)
 with db.connect() as c:c.execute("UPDATE product_listing_controls SET state='waiting' WHERE state='running' AND updated<?",(time.time()-360,))
 while True:
  from .pipeline_modules.control import paused
  if paused(db,'publication'):
   await asyncio.sleep(2);continue
  try:worked=await tick(db)
  except Exception:db.health('listing-control-error');worked=False
  await asyncio.sleep(.2 if worked else 2)


async def restore(db,key,body,api,cfg,targets,blocks):
 prohibited={tuple(x) for x in blocks['offers']};pending=False
 old={(x['shop_id'],x['offer_id']):x for x in body.get('targets',[])}
 body['targets']=[old.get((t['shop_id'],t['offer_id']),t) for t in targets]
 for target in body['targets']:
  row=await online(api,target)
  if not row:raise ValueError('导入记录存在，但尚未回查到原在线商品')
  if str(row.get('sku')) in blocks['skus'] or (target['shop_id'],target['offer_id']) in prohibited or ('*',target['offer_id']) in prohibited:raise ValueError('该商品在飞书明确下架清单中')
  if row.get('online_status')=='archived' or row.get('archived_type') in ('manual','auto'):
   if not target.get('restore_dispatched'):
    target['restore_dispatched']=True;save(db,key,'running',body)
    await api.erp('POST','/api.product.online/unarchive',body={'ids':[int(row['id'])]})
    await api.erp('POST','/api.product.online/sync_shop',body={'ids':[int(target['shop_id'])],'type':'all'})
   pending=True;continue
  from .plugin_publication import classify_product_issues
  issues=row.get('errors') or []
  if not isinstance(issues,list) or not all(isinstance(e,dict) for e in issues):
   raise ValueError('平台商品错误信息不完整，需重新核对')
  if classify_product_issues(tuple(str(e.get('code') or 'unknown_platform_issue') for e in issues),
      issues=issues,status=str(row.get('online_status') or ''),primary_image=row.get('primary_image') or ''):
   raise ValueError('平台商品存在错误，需修复后才能恢复可售')
  if issues:target['nonblocking_platform_warnings']=issues
  shops=await api.rows('/api.shop/lists',{'scene':'erp'});shop=next((s for s in shops if str(s['id'])==target['shop_id']),None)
  warehouses=[w for w in (shop or {}).get('warehouse',[]) if '嘉兴邮政' in w.get('name','')]
  if len(warehouses)!=1:raise ValueError('未能唯一确认原店铺的嘉兴邮政仓')
  warehouse=str(warehouses[0]['warehouse_id'])
  data=await api.erp('GET','/api.product.online/get_stock',params={'id':int(row['id'])});stocks=data if isinstance(data,list) else data.get('data',[])
  for st in stocks:
   if str(st.get('offer_id'))!=target['offer_id'] or str(st.get('product_id'))!=str(row.get('product_id')):raise ValueError('库存回查商品身份不一致')
  if row.get('online_status')=='selling' and any(str(st.get('warehouse_id'))==warehouse and int(st.get('present') or 0)>0 for st in stocks):
   target['selling_verified']=True;continue
  await api.erp('POST','/api.product.online/batch_update_stock',body={'shop_id':int(target['shop_id']),'products':[{'id':int(row['id']),'offer_id':target['offer_id'],'warehouses':[{'warehouse_id':int(warehouse),'stock':99}]}]})
  pending=True
 return ('waiting' if pending else 'listed'),body


def management_card(c,owner,row,sku,seller):
 from .identity_review import card as identity_card
 from .manual_reviews import revision,chinese_reason
 item=identity_card(c,owner,row,sku,seller,revision(row))
 control=row.get('listing_control') or {};detail=json.loads(control.get('body') or '{}')
 item.update(pipeline_state=row['state'],listing_action=control.get('action'),listing_state=control.get('state'),listing_error=chinese_reason(detail.get('error') or detail.get('reason')) if detail.get('error') or detail.get('reason') else None,listing_targets=detail.get('targets',[]))
 if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone():
  store=c.execute("SELECT s.name FROM stores s WHERE s.owner=? AND s.id=COALESCE((SELECT json_extract(body,'$.store_id') FROM plugin_publications WHERE owner=? AND sku=? AND seller=?),(SELECT store_id FROM plugin_routes WHERE owner=? AND sku=? AND seller=?))",(owner,owner,sku,seller,owner,sku,seller)).fetchone()
 else:store=c.execute('SELECT s.name FROM plugin_routes r JOIN stores s ON s.id=r.store_id AND s.owner=r.owner WHERE r.owner=? AND r.sku=? AND r.seller=?',(owner,sku,seller)).fetchone()
 item['target_store_name']=store[0] if store else '待核对目标店铺'
 item['store_switches']=detail.get('store_switches',[])
 active=bool(c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,seller,time.time())).fetchone()) or control.get('state') in ('running','waiting','queued','hold_queued','hold_waiting')
 item['can_list']=item['can_unlist']=not active
 if control.get('action')=='list' and control.get('state') in ('queued','waiting') and not c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,seller,time.time())).fetchone():item['can_unlist']=True
 item['allowed_actions']=([a for a in item['allowed_actions'] if row['state']=='needs_review'] if not active else [])+(['list'] if item['can_list'] else [])+(['unlist'] if item['can_unlist'] else [])
 item['can_approve']='approve' in item['allowed_actions'];item['can_repair']='repair' in item['allowed_actions']
 labels={'quarantined':'已隔离 · 不占正常上架队列','selling':'已上架','same_product_confirmed':'同款已确认','rejected':'已拒绝','delisting':'下架处理中','delisted':'已下架','not_listed':'未查到本店上架记录','publishing':'上架处理中'}
 item['category_label']=labels.get(row['state'],item['category_label'])
 return item
