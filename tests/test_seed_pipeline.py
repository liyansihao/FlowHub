import asyncio,json,sqlite3
import pytest
from flowhub import seed_pipeline as p


def test_empty_sales_keeps_detail_and_lists_only_missing_fields():
 product={'title':'x','image':'https://example.com/a','url':'https://example.com','plugin_detail':{'weight_g':20,'dimensions_mm':[150,180,10],'attributes':[{'id':1}]}}
 assert p.fields(product)==['sale_price_with_currency']

@pytest.mark.asyncio
async def test_collection_continues_while_evaluation_is_running(tmp_path,monkeypatch):
 monkeypatch.setattr(p,'RUN',tmp_path);monkeypatch.setattr(p,'stop',False);monkeypatch.setattr(p,'status',lambda:{})
 with p.connect() as c:
  c.execute('CREATE TABLE items(sku TEXT,seller TEXT,position INTEGER,state TEXT,reason TEXT,collect_seconds REAL,eval_seconds REAL,collection TEXT,report TEXT,updated REAL)')
  p.repair_schema(c)
  c.executemany("INSERT INTO items(sku,seller,position,state) VALUES(?,?,?,'pending')",[('1','2',1),('2','2',2)])
 evaluating=asyncio.Event();second_collected=asyncio.Event()
 async def collect(db,row):
  if row['sku']=='2':
   await asyncio.wait_for(evaluating.wait(),2);second_collected.set()
  return 'ready','',{}
 async def evaluate(db,owner,sku,seller):
  if sku=='1':
   evaluating.set();await asyncio.wait_for(second_collected.wait(),2)
  return {'state':'matched'}
 monkeypatch.setattr(p,'collect',collect);monkeypatch.setattr(p,'evaluate',evaluate)
 await asyncio.wait_for(asyncio.gather(p.produce(None),p.consume(None)),5)
 with p.connect() as c:assert c.execute("SELECT count(*) FROM items WHERE state='matched'").fetchone()[0]==2

@pytest.mark.asyncio
async def test_missing_fields_are_repaired_then_evaluated_once(tmp_path,monkeypatch):
 monkeypatch.setattr(p,'RUN',tmp_path);monkeypatch.setattr(p,'stop',False)
 monkeypatch.setattr(p,'status',lambda:{})
 monkeypatch.setattr(p,'repair_delay',lambda attempt:0)
 with p.connect() as c:
  c.execute('CREATE TABLE items(sku TEXT,seller TEXT,position INTEGER,state TEXT,reason TEXT,collect_seconds REAL,eval_seconds REAL,collection TEXT,report TEXT,updated REAL)')
  p.repair_schema(c)
  c.execute("INSERT INTO items(sku,seller,position,state) VALUES('1','2',1,'pending')")
 calls=[]
 async def collect(db,row):
  calls.append(row['state'])
  if len(calls)==1:return 'needs_fields','price_rub',{'missing_fields':['price_rub']}
  return 'ready','',{'missing_fields':[]}
 evaluated=[]
 async def evaluate(*args):evaluated.append(1);return {'state':'matched'}
 monkeypatch.setattr(p,'collect',collect);monkeypatch.setattr(p,'evaluate',evaluate)
 await asyncio.wait_for(asyncio.gather(p.produce(None),p.consume(None)),5)
 assert calls==['pending','repair_pending'] and len(evaluated)==1
 with p.connect() as c:
  assert c.execute('SELECT state FROM items').fetchone()[0]=='matched'
  assert c.execute('SELECT count(*) FROM repair_events').fetchone()[0]==2

def test_repair_delay_is_bounded():
 assert [p.repair_delay(n) for n in (1,2,3,100)]==[60,120,240,3600]

def test_maozi_cache_must_bind_seller_and_have_current_complete_sales():
 product={'sku':'1','seller_id':'2','plugin_detail':{}}
 packet={'maozi':{'observed_at':100,'result':{'status':{'update_sales':False},'data':{'sku':'1','sellerId':'2','avgPrice':200,'salesSchema':'FBS','blockedBySeller':False}}}}
 packet['maozi']['result']['data']['sellerId']='3'
 assert not p.merge_cached_sales(product,packet)
 packet['maozi']['result']['data']['sellerId']='2'
 packet['maozi']['result']['status']['update_sales']=True
 assert not p.merge_cached_sales(product,packet)
 packet['maozi']['result']['status']['update_sales']=False
 assert p.merge_cached_sales(product,packet)
 assert product['plugin_detail']['monthly_sales']['average_price_rub']==200
