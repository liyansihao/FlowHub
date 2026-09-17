import json,time
import pytest
from test_manual_reviews import setup,rev
from flowhub.manual_reviews import decide,load
from flowhub.listing_controls import schema,request,management_card


def test_listing_rights_survive_review_and_selling(tmp_path):
 db,o=setup(tmp_path);schema(db)
 with db.connect() as c:
  c.execute("UPDATE plugin_pipeline SET state='selling'")
  item=management_card(c,o,load(c,o,'123','456'),'123','456')
 assert 'list' in item['allowed_actions'] and 'unlist' in item['allowed_actions']
 assert 'approve' not in item['allowed_actions']
 out=decide(db,o,'user','123','456','unlist','下架',rev(db,o))
 assert out['state']=='queued'
 with db.connect() as c:assert c.execute('SELECT count(*) FROM plugin_pipeline').fetchone()[0]==1


def test_opposite_action_waits_for_unknown_result(tmp_path):
 db,o=setup(tmp_path);schema(db)
 with db.connect() as c:
  request(c,o,'123','456','unlist','user','下架')
  c.execute("UPDATE product_listing_controls SET state='waiting'")
  with pytest.raises(ValueError):request(c,o,'123','456','list','user','上架')


@pytest.mark.asyncio
async def test_unlist_requires_zero_stock_before_archive(tmp_path,monkeypatch):
 import flowhub.listing_controls as m
 db,o=setup(tmp_path);calls=[]
 class Api:
  async def rows(self,path,params):return [{'id':1,'warehouse':[{'warehouse_id':99}]}]
  async def erp(self,method,path,**kwargs):
   calls.append(path)
   if path.endswith('get_stock'):return [{'offer_id':'x','product_id':2,'warehouse_id':99,'present':3}]
   return {}
 monkeypatch.setattr(m,'context',lambda *a:(Api(),{'shop_id':1},None,{}))
 async def online(*a):return {'id':1,'sku':'123','product_id':2,'stock':3,'online_status':'selling'}
 monkeypatch.setattr(m,'online',online)
 state,result=await m.remove(db,(o,'123','456'),{'targets':[{'shop_id':'1','offer_id':'x'}]})
 assert state=='waiting' and '/api.product.online/archive' not in calls
 assert '/api.product.online/batch_update_stock' in calls


@pytest.mark.parametrize('evidence',['human_review','automatic_review'])
def test_website_listing_uses_confirmed_identity_without_profit_threshold(tmp_path,evidence):
 from test_plugin_publication import report
 from flowhub.plugin_publication import approved
 r=report();r['website_listing_authorization']={'id':'explicit-user-order'}
 r['identity_review']={'verdict':'match',evidence:{'actor':'user'}}
 r['result']['evidence']['profit']['assessment']['erp_profit_rate_pct']=10
 r['publication_blockers']=['publication_attributes_missing']
 assert approved(r,{'profit_min':30},now=101)
 r.pop('website_listing_authorization')
 with pytest.raises(ValueError):approved(r,{'profit_min':30},now=101)


def test_queued_listing_can_be_cancelled_by_unlisting(tmp_path):
 db,o=setup(tmp_path);schema(db)
 with db.connect() as c:
  request(c,o,'123','456','list','user','上架')
  assert request(c,o,'123','456','unlist','user','撤销上架并下架')['action']=='unlist'


@pytest.mark.asyncio
async def test_targeted_listing_only_processes_exact_requested_product(tmp_path,monkeypatch):
 import flowhub.listing_controls as m
 db,o=setup(tmp_path);schema(db);calls=[]
 with db.connect() as c:
  request(c,o,'999','456','list','test','unrelated existing request')
  request(c,o,'123','456','list','test','requested cohort')
 async def prepare(db,key,body):calls.append(key);return 'listed',body
 monkeypatch.setattr(m,'prepare_listing',prepare)
 assert await m.tick(db,target=(o,'123','456'))
 assert calls==[(o,'123','456')]
 with db.connect() as c:
  assert c.execute("SELECT state FROM product_listing_controls WHERE sku='999'").fetchone()[0]=='queued'
  assert c.execute("SELECT state FROM product_listing_controls WHERE sku='123'").fetchone()[0]=='listed'
 with pytest.raises(ValueError):await m.tick(db,target=('', '123','456'))


@pytest.mark.asyncio
@pytest.mark.parametrize('level,status,primary,code,passes',[
 ('ERROR_LEVEL_WARNING','ready_to_sell','https://example.com/p.jpg','pics_http_error',True),
 ('ERROR_LEVEL_ERROR','ready_to_sell','https://example.com/p.jpg','invalid_size',False),
 ('ERROR_LEVEL_WARNING','unknown','https://example.com/p.jpg','pics_http_error',False),
 ('ERROR_LEVEL_WARNING','ready_to_sell','','pics_http_error',False),
 ('ERROR_LEVEL_WARNING','ready_to_sell','https://example.com/p.jpg','warning_all_image_failed',False),
])
async def test_restore_respects_existing_platform_warning_policy(tmp_path,monkeypatch,level,status,primary,code,passes):
 import flowhub.listing_controls as m
 db,o=setup(tmp_path);writes=[]
 class Api:
  async def rows(self,*args):return [{'id':1,'warehouse':[{'warehouse_id':99,'name':'嘉兴邮政'}]}]
  async def erp(self,method,path,**kwargs):
   if method=='POST':writes.append(path)
   if path.endswith('get_stock'):return [{'offer_id':'x','product_id':2,'warehouse_id':99,'present':0}]
   return {}
 async def online(*args):return {'id':1,'sku':'123','product_id':2,'online_status':status,'primary_image':primary,'errors':[{'code':code,'level':level,'state':'new'}]}
 monkeypatch.setattr(m,'online',online)
 args=(db,(o,'123','456'),{},Api(),{'shop_id':1},[{'shop_id':'1','offer_id':'x'}],{'skus':[],'offers':[]})
 if passes:
  state,body=await m.restore(*args)
  assert state=='waiting' and writes==['/api.product.online/batch_update_stock']
 else:
  with pytest.raises(ValueError):await m.restore(*args)
  assert not writes
