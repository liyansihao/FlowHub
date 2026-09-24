import json,time
import pytest
from flowhub.db import Database
from flowhub.pipeline_modules.admission import schema as pipeline_schema
from flowhub.pipeline_modules import draft_cleanup as cleanup


def setup(tmp_path):
 db=Database(tmp_path);pipeline_schema(db);cleanup.schema(db)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True}))
 with db.connect() as c:
  owner=c.execute('SELECT id FROM users').fetchone()[0]
  c.execute('INSERT INTO pipeline_campaigns VALUES(?,?,?,?)',(owner,1,'{}',0))
  c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'123','2','selling',json.dumps({'offer_id':'offer'}),0,0))
 item={'sku':'123','seller':'2','snapshot':{'draft_id':8,'favorite_id':7,'source_key':'123','detail':{'skus':[{}]}},'queue':{'offer_id':'offer'},'context':{'store':{'config':{'shop_id':'4'}}},'source_record':'key'}
 return db,owner,item


class Fake:
 def __init__(self,mode='ok'):self.present=True;self.writes=0;self.mode=mode
 async def call(self,path,method='GET',params=None):
  if path.endswith('collect/lists'):
   return {'used':1000,'limit':1000,'total':1 if self.present else 0,'data':[{'id':8,'goods_id':'123','collect_from':'ozon'}] if self.present else []}
  if path.endswith('online/lists'):
   return {'data':[{'shop_id':'4','offer_id':'offer','online_status':'selling','stock':0 if self.mode=='no_stock' else 99}]}
  if path.endswith('/detail'):return {'skus':[{}],'title':'backup'}
  assert path=='/api.product.collect/del' and method=='DELETE' and params=={'ids':'8'}
  self.writes+=1
  if self.mode=='unknown_present':raise TimeoutError()
  self.present=False
  if self.mode=='lost_ack':raise TimeoutError()
  return {}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['ok','lost_ack','unknown_present','no_stock'])
async def test_cleanup_exact_backup_and_unknown_delete_never_replayed(tmp_path,mode):
 db,owner,item=setup(tmp_path);api=Fake(mode)
 result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
 with db.connect() as c:r=c.execute('SELECT * FROM draft_cleanup_receipts').fetchone()
 if mode=='no_stock':assert r is None and api.writes==0
 else:
  assert db.open(r['body'])['fresh_detail']['title']=='backup'
  assert r['state']==('unconfirmed' if mode=='unknown_present' else 'deleted')
  assert result['deleted']==(0 if mode=='unknown_present' else 1)
 await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
 assert api.writes==(0 if mode=='no_stock' else 1)


@pytest.mark.asyncio
async def test_pause_prevents_delete_after_reads(tmp_path):
 from flowhub.pipeline_modules.control import set_paused
 db,owner,item=setup(tmp_path);api=Fake();set_paused(db,'seed',True)
 await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
 assert api.writes==0


@pytest.mark.asyncio
async def test_certified_released_favorite_draft_is_never_deleted(tmp_path):
 db,owner,item=setup(tmp_path);api=Fake()
 with db.connect() as c:
  c.execute('''CREATE TABLE favorite_release_certificates(
   account TEXT,favorite_id TEXT,sku TEXT,source_key TEXT,draft_id TEXT,
   state TEXT,proof TEXT,updated REAL,PRIMARY KEY(account,favorite_id))''')
  c.execute('INSERT INTO favorite_release_certificates VALUES(?,?,?,?,?,?,?,?)',
            ('account','7','123','source','8','deleted','{}',time.time()))
 result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
 assert result['deleted']==0 and api.writes==0
 with db.connect() as c:assert c.execute('SELECT count(*) FROM draft_cleanup_receipts').fetchone()[0]==0


@pytest.mark.asyncio
async def test_mismatched_source_and_below_threshold_do_not_delete(tmp_path):
 db,owner,item=setup(tmp_path);api=Fake();item['sku']='999'
 await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
 assert api.writes==0
 class Low(Fake):
  async def call(self,path,method='GET',params=None):
   d=await super().call(path,method,params)
   if path.endswith('collect/lists'):d['used']=800
   return d
 api=Low();item['sku']='123'
 assert (await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api))['state']=='below_threshold'
 assert api.writes==0


@pytest.mark.parametrize('kind',['eligible','recent','recovered','no_creation_proof'])
def test_only_old_locally_created_sold_drafts_are_candidates(tmp_path,kind):
 from flowhub.source_detail import SourceCollector
 db,owner,item=setup(tmp_path)
 ctx={'owner':owner,'candidate':{'source_key':'123'},'store':{'config':{'shop_id':'4'},'credentials':{'erp_token':'test'}}}
 with db.connect() as c:
  c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',('s',owner,'test','maozi',json.dumps(ctx['store']['config']),db.seal(ctx['store']['credentials'])))
  c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,'123','2','s',0,'test'))
 collector=SourceCollector(db,ctx);snapshot=item['snapshot']
 if kind=='recovered':snapshot['recovery']={'source':'exact-ozon-draft-list'}
 if kind=='no_creation_proof':snapshot.pop('favorite_id')
 collector.save('ready',snapshot)
 if kind!='recent':
  with db.connect() as c:c.execute('UPDATE source_details SET updated=?',(time.time()-7200,))
 assert bool(cleanup.candidates(db,owner,cleanup.config(db)))==(kind=='eligible')


def test_paged_candidate_scan_advances_and_wraps_without_full_queue_walk(tmp_path):
 from flowhub.source_detail import SourceCollector
 db,owner,item=setup(tmp_path)
 with db.connect() as c:
  c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',
            ('s',owner,'test','maozi','{"shop_id":"4"}',db.seal({'erp_token':'test'})))
  for sku in ('123','124','125'):
   if sku!='123':c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,sku,'2','selling','{"offer_id":"offer"}',0,0))
   c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,sku,'2','s',0,'test'))
 ctx={'owner':owner,'candidate':{'source_key':'125'},'store':{'config':{'shop_id':'4'},'credentials':{'erp_token':'test'}}}
 SourceCollector(db,ctx).save('ready',{'draft_id':8,'favorite_id':7,'source_key':'125','detail':{'skus':[{}]}})
 with db.connect() as c:c.execute('UPDATE source_details SET updated=?',(time.time()-7200,))
 settings=cleanup.config(db)|{'paged_scan':True,'candidate_page_size':1}
 seen=[]
 for _ in range(4):
  groups=cleanup.candidates(db,owner,settings)
  seen.extend(item['sku'] for items in groups.values() for item in items)
 assert seen==['125']
 with db.connect() as c:
  assert c.execute('SELECT last_rowid FROM draft_cleanup_scan_cursors WHERE owner=?',(owner,)).fetchone()[0]>0


@pytest.mark.asyncio
async def test_empty_candidate_page_keeps_paged_scan_cadence(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,'interval_seconds':300}))
 monkeypatch.setattr(cleanup,'candidates',lambda *args:{'account':[item]})
 async def empty(*args,**kwargs):return {'state':'no_safe_candidates','deleted':0,'examined':0}
 monkeypatch.setattr(cleanup,'clean_account',empty)
 assert (await cleanup.tick(db))[0]['next_scan_after_seconds']==300
 with db.connect() as c:
  assert c.execute('SELECT failures FROM draft_cleanup_backoff WHERE account="account"').fetchone()[0]==0


@pytest.mark.asyncio
async def test_backed_off_account_does_not_lose_candidate_page(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True}))
 with db.connect() as c:
  c.execute('CREATE TABLE draft_cleanup_backoff(account TEXT PRIMARY KEY,failures INTEGER,due REAL)')
  c.execute('INSERT INTO draft_cleanup_backoff VALUES(?,?,?)',('account',7,time.time()+1800))
 def page(db,owner,settings):
  with db.connect() as c:c.execute('INSERT OR REPLACE INTO draft_cleanup_scan_cursors VALUES(?,?)',(owner,12))
  return {'account':[item]}
 monkeypatch.setattr(cleanup,'candidates',page)
 assert await cleanup.tick(db)==[]
 with db.connect() as c:
  assert c.execute('SELECT last_rowid FROM draft_cleanup_scan_cursors WHERE owner=?',(owner,)).fetchone() is None


@pytest.mark.asyncio
async def test_unstable_pagination_cannot_prove_absence():
 class Moving:
  async def call(self,path,method='GET',params=None):
   return {'total':101,'data':[{'id':i} for i in range(100)] if params['page']==1 else [{'id':99}]}
 with pytest.raises(ValueError,match='unstable'):await cleanup.listing(Moving())


@pytest.mark.asyncio
async def test_clear_completed_does_not_stop_at_capacity_target(tmp_path):
 db,owner,item=setup(tmp_path)
 class Low(Fake):
  async def call(self,*args,**kwargs):
   r=await super().call(*args,**kwargs)
   if args[0].endswith('collect/lists'):r['used']=100
   return r
 api=Low()
 result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db)|{'clear_completed':True,'batch_size':1000},api)
 assert result['deleted']==1 and api.writes==1


@pytest.mark.asyncio
@pytest.mark.parametrize('guard',[None,'binding','wrong_offer','zero_stock','wrong_warehouse','other_inflight','missing_record'])
async def test_official_sold_draft_cleanup_uses_fresh_official_stock_without_erp_online(tmp_path,monkeypatch,guard):
 import httpx
 from flowhub import official_api
 db,owner,item=setup(tmp_path)
 item['queue']['backend']='official'
 item['context']['store'].update(config={'shop_id':'4','warehouse_id':'5'},credentials={'client_id':'6','api_key':'test'})
 with db.connect() as c:
  c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
  record={'backend':'official','offer_id':'offer','account_binding':['wrong' if guard=='binding' else '6','5','4']}
  if guard!='missing_record':c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?)',(owner,'123','2',json.dumps(record)))
  if guard=='other_inflight':c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'123','3','needs_fields','{}',0,0))
 calls=[]
 def reply(request):
  calls.append(request.url.path)
  if request.url.path=='/v2/warehouse/list':return httpx.Response(200,json={'warehouses':[{'warehouse_id':5,'status':'active'}]})
  if request.url.path=='/v3/product/info/list':return httpx.Response(200,json={'items':[{'id':10,'offer_id':'other' if guard=='wrong_offer' else 'offer','sku':20,'statuses':{'status_name':'selling'}}]})
  assert request.url.path=='/v2/product/info/stocks-by-warehouse/fbs'
  return httpx.Response(200,json={'products':[{'sku':20,'product_id':10,'offer_id':'offer','warehouse_id':9 if guard=='wrong_warehouse' else 5,'present':0 if guard=='zero_stock' else 99,'reserved':0}]})
 monkeypatch.setattr(official_api,'client',lambda db,keys:httpx.AsyncClient(base_url='https://api-seller.ozon.ru',transport=httpx.MockTransport(reply)))
 class OfficialOnly(Fake):
  async def call(self,path,method='GET',params=None):
   assert not path.endswith('online/lists')
   return await super().call(path,method,params)
 api=OfficialOnly()
 result=await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
 assert api.writes==(0 if guard else 1)
 if not guard:
  assert result['deleted']==1
  with db.connect() as c:r=c.execute('SELECT body FROM draft_cleanup_receipts').fetchone()
  assert db.open(r[0])['online']['backend']=='official'
  await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
  assert api.writes==1
