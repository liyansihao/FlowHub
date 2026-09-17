import asyncio
import pytest
from flowhub.sellable_audit_policy import policy,sales_index,historical_sales,delist_step

def test_only_verified_zero_sales_mismatch_can_auto_remove():
 assert policy('mismatch',0,True)=='queued'
 for args in [('match',0,True),('uncertain',0,True),('mismatch',None,False),('mismatch',0,False),('mismatch',1,True)]:assert policy(*args)=='needs_review'
 assert policy('mismatch',3,True,explicit=True)=='queued'

def test_sales_are_own_shop_all_non_cancelled_and_deduplicated():
 a={'posting_number':'a','shop_id':1,'status':'delivered','products':[{'offer_id':'x','sku':10,'quantity':2}]}
 b={**a,'posting_number':'b','status':'awaiting_deliver'}
 c={**a,'posting_number':'c','status':'cancelled'}
 index=sales_index([a,a,b,c]);assert historical_sales({'shop_id':1,'offer_id':'x','sku':10},index)==4
 assert historical_sales({'shop_id':2,'offer_id':'x','sku':10},index)==0
 with pytest.raises(ValueError):sales_index([{**a,'products':None}])

class Fake:
 def __init__(self):
  self.p={'id':1,'sku':2,'product_id':3,'shop_id':4,'offer_id':'own','name':'title','primary_image':'image','stock':99,'online_status':'selling'};self.writes=[];self.stock=99
 async def rows(self,path,params):
  if path.endswith('/lists') and 'online' in path:return [dict(self.p)]
  return [{'id':4,'warehouse':[{'warehouse_id':5},{'warehouse_id':6}]}]
 async def erp(self,method,path,body=None,params=None):
  if method=='GET':return [{'offer_id':'own','product_id':3,'warehouse_id':5,'present':self.stock}]
  self.writes.append((path,body));return {}

def test_delist_requires_zero_readback_then_archive_readback():
 async def run():
  a=Fake();original=dict(a.p);receipt={}
  assert await delist_step(a,original,receipt,lambda:None)=='waiting'
  assert len(a.writes)==1 and len(a.writes[0][1]['products'][0]['warehouses'])==2
  a.stock=0;a.p['stock']=0
  assert await delist_step(a,original,receipt,lambda:None)=='waiting'
  assert any('archive' in path for path,_ in a.writes)
  count=len(a.writes)
  assert await delist_step(a,original,receipt,lambda:None)=='waiting'
  assert len(a.writes)==count
  a.p['archived_type']='manual'
  assert await delist_step(a,original,receipt,lambda:None)=='delisted'
 asyncio.run(run())

def test_changed_own_identity_prevents_any_write():
 async def run():
  a=Fake();original=dict(a.p);a.p['sku']=999
  with pytest.raises(ValueError):await delist_step(a,original,{},lambda:None)
  assert not a.writes
 asyncio.run(run())

def test_pending_sales_card_decision_is_revision_bound_and_does_not_publish(tmp_path):
 from flowhub.db import Database
 from flowhub.sellable_audit_policy import schema,put,cards,decide
 db=Database(tmp_path/'db');schema(db)
 p={'sku':12,'id':1,'shop_id':2,'shop_name':'Store','offer_id':'own','name':'Own product','primary_image':'https://example.com/a.jpg'}
 identity={'verdict':'mismatch','comparison':{'decision':{},'search_and_rank':{'candidates':[]}}}
 assert put(db,'owner',p,identity,2,True)
 with db.connect() as c:card=cards(c,'owner')[0]
 assert card['pipeline_state']=='needs_review' and card['historical_sales']==2
 with pytest.raises(ValueError):decide(db,'owner','user','12','owned-audit:2','reject','test','stale')
 assert decide(db,'owner','user','12','owned-audit:2','approve','retain',card['revision'])['state']=='retained'
 with db.connect() as c:
  assert c.execute('select state from sellable_audit_cards').fetchone()[0]=='retained'
  assert not c.execute('select * from jobs').fetchall()

def test_missing_brand_is_not_explicit_conflict_for_automatic_delisting():
 from flowhub.sellable_audit_policy import contradictory_brand_evidence
 identity={'comparison':{'decision':{'qwen_review':{'brand_or_model_conflict':True,'reason':'Ozon商品无品牌标识，1688商品明确标注品牌，属于明确品牌冲突'}}}}
 assert contradictory_brand_evidence(identity)

def test_uncertain_zero_sales_stays_in_review_and_keeps_human_decision(tmp_path):
 from flowhub.db import Database
 from flowhub.sellable_audit_policy import schema,put,cards,decide
 db=Database(tmp_path/'db');schema(db)
 p={'sku':12,'id':1,'shop_id':2,'shop_name':'Store','offer_id':'own','name':'Own product','primary_image':'https://example.com/a.jpg'}
 identity={'verdict':'uncertain','comparison':{'decision':{},'search_and_rank':{'candidates':[]}}}
 assert put(db,'owner',p,identity,0,True)
 with db.connect() as c:card=cards(c,'owner')[0]
 assert card['pipeline_state']=='needs_review'
 assert card['category_label']=='识别不确定 · 待人工审核'
 assert '自动下架' not in card['reason']
 decide(db,'owner','user','12','owned-audit:2','approve','保留',card['revision'])
 assert not put(db,'owner',p,identity,0,True)
 with db.connect() as c:assert c.execute('select state from sellable_audit_cards').fetchone()[0]=='retained'
