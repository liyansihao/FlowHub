import json,time
import pytest
from test_manual_reviews import setup
from flowhub.listing_controls import schema,request
from flowhub import listing_fallback as module


def fixture(tmp_path,monkeypatch,quota=None):
 db,o=setup(tmp_path);schema(db)
 cfg={'shop_id':'2','warehouse_id':'22','watermark_id':'33'}
 with db.connect() as c:
  c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))')
  for sid,name,config in [('old','原店',{'shop_id':'1'}),('new','备用店',cfg)]:
   c.execute('INSERT INTO stores(id,owner,name,kind,config,secret,verified,enabled,position) VALUES(?,?,?,?,?,?,1,0,0)',(sid,o,name,'maozi',json.dumps(config),db.seal({'erp_token':'test'})))
  c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(o,'123','456','old',time.time()+100,'test'))
  c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(o,'other','seller','new',time.time()+100,'test'))
  request(c,o,'123','456','list','user','上架')
  body=json.loads(c.execute('SELECT body FROM product_listing_controls').fetchone()[0])
 class API:
  async def erp(self,*a,**kw):return quota or {'total':'10/100','daily_create':'10/100'}
  async def rows(self,path,params):
   if path=='/api.shop/lists':return [{'id':2,'status':1,'currency':'CNY','watermark_id':33,'warehouse':[{'warehouse_id':22,'name':'嘉兴邮政'}]}]
   return []
 monkeypatch.setattr(module,'MaoziPublisher',lambda *a:API())
 return db,o,body,API(),{'id':'old','name':'原店'}


@pytest.mark.asyncio
async def test_full_store_switch_is_atomic_and_audited(tmp_path,monkeypatch):
 db,o,body,api,old=fixture(tmp_path,monkeypatch)
 result=await module.choose(db,(o,'123','456'),body,api,old,{'total':'0/100','daily_create':'10/100'})
 assert result[3]['id']=='new'
 with db.connect() as c:
  assert c.execute("SELECT store_id FROM plugin_routes WHERE sku='123'").fetchone()[0]=='new'
  assert json.loads(c.execute('SELECT body FROM product_listing_controls').fetchone()[0])['store_switches'][0]['from_store_id']=='old'


@pytest.mark.asyncio
async def test_all_full_waits_and_does_not_change_route(tmp_path,monkeypatch):
 db,o,body,api,old=fixture(tmp_path,monkeypatch,{'total':'0/100','daily_create':'10/100'})
 assert await module.choose(db,(o,'123','456'),body,api,old,{'total':'0/100','daily_create':'10/100'}) is None
 assert body['phase']=='capacity_wait' and body['next_attempt_at']>time.time()
 with db.connect() as c:assert c.execute("SELECT store_id FROM plugin_routes WHERE sku='123'").fetchone()[0]=='old'


@pytest.mark.asyncio
async def test_existing_publication_never_moves(tmp_path,monkeypatch):
 db,o,body,api,old=fixture(tmp_path,monkeypatch)
 with db.connect() as c:c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?,?)',(o,'123','other-seller','{}',time.time()))
 with pytest.raises(ValueError,match='已有发布记录'):await module.choose(db,(o,'123','456'),body,api,old,{'total':'0/100','daily_create':'10/100'})


@pytest.mark.asyncio
async def test_existing_remote_import_prevents_switch(tmp_path,monkeypatch):
 db,o,body,api,old=fixture(tmp_path,monkeypatch)
 import flowhub.listing_controls as controls
 async def existing(*a):return [{'shop_id':'2','offer_id':'existing'}]
 monkeypatch.setattr(controls,'owned_targets',existing)
 with pytest.raises(ValueError,match='导入记录'):await module.choose(db,(o,'123','456'),body,api,old,{'total':'0/100','daily_create':'10/100'})


def test_unavailable_quota_is_not_assumed_full():
 assert not module.exhausted({})
 assert not module.exhausted({'total':'unknown','daily_create':'0/100'})
 assert module.exhausted({'total':'50/100','daily_create':'0/100'})


@pytest.mark.asyncio
async def test_capacity_wait_respects_retry_deadline(tmp_path,monkeypatch):
 import flowhub.listing_controls as controls
 db,o,body,api,old=fixture(tmp_path,monkeypatch)
 body.update(phase='capacity_wait',next_attempt_at=time.time()+300)
 with db.connect() as c:c.execute("UPDATE product_listing_controls SET state='waiting',body=?,updated=0",(json.dumps(body),))
 async def forbidden(*a):pytest.fail('should wait until retry deadline')
 monkeypatch.setattr(controls,'prepare_listing',forbidden)
 assert await controls.tick(db) is False
