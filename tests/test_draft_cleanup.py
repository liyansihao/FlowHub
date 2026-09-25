import asyncio,json,threading,time
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
 if mode=='no_stock':
  assert r is None and api.writes==0
  assert result['ineligible']==1 and result['read_errors']==0
 else:
  assert db.open(r['body'])['fresh_detail']['title']=='backup'
  assert r['state']==('unconfirmed' if mode=='unknown_present' else 'deleted')
  assert result['deleted']==(0 if mode=='unknown_present' else 1)
 await cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api)
 assert api.writes==(0 if mode=='no_stock' else 1)


@pytest.mark.asyncio
async def test_capacity_observation_does_not_block_cleanup_event_loop(tmp_path,monkeypatch):
 from flowhub import collection_capacity
 db,owner,item=setup(tmp_path);api=Fake()
 original=collection_capacity.observe
 calls=[]
 def slow_observe(*args):
  calls.append(threading.get_ident())
  time.sleep(.15)
  return original(*args)
 monkeypatch.setattr(collection_capacity,'observe',slow_observe)
 task=asyncio.create_task(cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api))
 start=time.monotonic()
 await asyncio.sleep(.03)
 assert time.monotonic()-start<.12
 result=await task
 assert result['deleted']==1
 assert len(calls)==2
 assert all(tid!=threading.get_ident() for tid in calls)


@pytest.mark.asyncio
async def test_contended_prior_receipt_read_does_not_block_cleanup_event_loop(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path);api=Fake()
 original=cleanup.prior_receipts
 loop=asyncio.get_running_loop();responded=loop.create_future();calls=[]
 def slow_receipts(*args):
  calls.append((threading.get_ident(),time.monotonic()))
  loop.call_soon_threadsafe(lambda:responded.set_result(time.monotonic()))
  time.sleep(.15)
  return original(*args)
 monkeypatch.setattr(cleanup,'prior_receipts',slow_receipts)
 task=asyncio.create_task(cleanup.clean_account(db,owner,'account',[item],cleanup.config(db),api))
 resumed_at=await asyncio.wait_for(responded,2)
 assert resumed_at-calls[0][1]<.12
 result=await task
 assert result['deleted']==1 and api.writes==1
 assert calls[0][0]!=threading.get_ident()


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
async def test_empty_candidate_page_advances_with_bounded_short_cadence(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,'interval_seconds':300}))
 monkeypatch.setattr(cleanup,'candidates',lambda *args:{'account':[item]})
 async def empty(*args,**kwargs):return {'state':'no_safe_candidates','deleted':0,'examined':0,'used_before':924,'limit':1000}
 monkeypatch.setattr(cleanup,'clean_account',empty)
 assert (await cleanup.tick(db))[0]['next_scan_after_seconds']==60
 with db.connect() as c:
  assert c.execute('SELECT failures FROM draft_cleanup_backoff WHERE account="account"').fetchone()[0]==0


@pytest.mark.asyncio
async def test_empty_page_at_low_capacity_keeps_normal_cadence(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,'interval_seconds':300}))
 monkeypatch.setattr(cleanup,'candidates',lambda *args:{'account':[item]})
 async def empty(*args,**kwargs):return {'state':'no_safe_candidates','deleted':0,'examined':0,'used_before':800,'limit':1000}
 monkeypatch.setattr(cleanup,'clean_account',empty)
 assert (await cleanup.tick(db))[0]['next_scan_after_seconds']==300


@pytest.mark.asyncio
async def test_empty_candidate_page_shortens_worker_sleep_only_for_empty_page(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,'interval_seconds':300}))
 delays=[]
 async def sleep(delay):
  delays.append(delay)
  raise asyncio.CancelledError()
 async def empty(*args,**kwargs):return [{'state':'no_safe_candidates','examined':0,'next_scan_after_seconds':60}]
 monkeypatch.setattr(cleanup,'tick',empty)
 monkeypatch.setattr(cleanup.asyncio,'sleep',sleep)
 with pytest.raises(asyncio.CancelledError):await cleanup.run(db)
 assert delays==[60]


@pytest.mark.asyncio
@pytest.mark.parametrize('used,age,expected_interval',[(924,0,60),(800,0,None),(924,3600,None)])
async def test_empty_local_candidate_page_shortens_only_under_fresh_capacity_pressure(tmp_path,used,age,expected_interval):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,
                                                        'candidate_page_size':1,'interval_seconds':300}))
 with db.connect() as c:
  c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',
            ('s',owner,'test','maozi','{"shop_id":"4"}',db.seal({'erp_token':'test'})))
  c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,'123','2','s',0,'test'))
  c.execute('CREATE TABLE collection_capacity(account TEXT PRIMARY KEY,used INTEGER,capacity INTEGER,observed REAL,blocked INTEGER)')
  c.execute('INSERT INTO collection_capacity VALUES(?,?,?,?,?)',('account',used,1000,time.time()-age,0))
 results=await cleanup.tick(db)
 assert ([r['next_scan_after_seconds'] for r in results] if results else None)==(
  [expected_interval] if expected_interval else None)
 with db.connect() as c:
  assert c.execute('SELECT last_rowid FROM draft_cleanup_scan_cursors WHERE owner=?',(owner,)).fetchone()[0]>0
  assert c.execute('SELECT count(*) FROM draft_cleanup_receipts').fetchone()[0]==0


@pytest.mark.asyncio
async def test_no_local_page_advance_keeps_normal_sleep_even_under_pressure(tmp_path):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,
                                                        'candidate_page_size':1,'interval_seconds':300}))
 with db.connect() as c:
  c.execute('CREATE TABLE collection_capacity(account TEXT PRIMARY KEY,used INTEGER,capacity INTEGER,observed REAL,blocked INTEGER)')
  c.execute('INSERT INTO collection_capacity VALUES(?,?,?,?,?)',('account',924,1000,time.time(),0))
 assert await cleanup.tick(db)==[]
 with db.connect() as c:
  assert c.execute('SELECT count(*) FROM draft_cleanup_scan_cursors').fetchone()[0]==0


@pytest.mark.asyncio
async def test_safely_ineligible_paged_candidates_advance_under_capacity_pressure(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,'interval_seconds':300}))
 monkeypatch.setattr(cleanup,'candidates',lambda *args:{'account':[item]})
 async def ineligible(*args,**kwargs):
  return {'state':'no_safe_candidates','deleted':0,'examined':2,'skipped':2,
          'read_errors':0,'ineligible':2,'used_before':924,'limit':1000}
 monkeypatch.setattr(cleanup,'clean_account',ineligible)
 result=(await cleanup.tick(db))[0]
 assert result['next_scan_after_seconds']==60 and result['short_page_scan'] is True
 with db.connect() as c:
  assert c.execute('SELECT failures FROM draft_cleanup_backoff WHERE account="account"').fetchone()[0]==0


@pytest.mark.asyncio
async def test_candidate_read_error_keeps_failure_backoff(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,'interval_seconds':300}))
 monkeypatch.setattr(cleanup,'candidates',lambda *args:{'account':[item]})
 async def failed_read(*args,**kwargs):
  return {'state':'no_safe_candidates','deleted':0,'examined':2,'skipped':2,
          'read_errors':1,'ineligible':1,'used_before':924,'limit':1000}
 monkeypatch.setattr(cleanup,'clean_account',failed_read)
 result=(await cleanup.tick(db))[0]
 assert result['next_scan_after_seconds']==600 and result['short_page_scan'] is False
 with db.connect() as c:
  assert c.execute('SELECT failures FROM draft_cleanup_backoff WHERE account="account"').fetchone()[0]==1


@pytest.mark.asyncio
async def test_ineligible_paged_candidates_keep_normal_cadence_below_pressure(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,'interval_seconds':300}))
 monkeypatch.setattr(cleanup,'candidates',lambda *args:{'account':[item]})
 async def ineligible(*args,**kwargs):
  return {'state':'no_safe_candidates','deleted':0,'examined':2,'skipped':2,
          'read_errors':0,'ineligible':2,'used_before':800,'limit':1000}
 monkeypatch.setattr(cleanup,'clean_account',ineligible)
 result=(await cleanup.tick(db))[0]
 assert result['next_scan_after_seconds']==300 and result['short_page_scan'] is False


@pytest.mark.asyncio
async def test_safely_ineligible_page_shortens_worker_sleep(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True,'interval_seconds':300}))
 delays=[]
 async def sleep(delay):
  delays.append(delay)
  raise asyncio.CancelledError()
 async def ineligible(*args,**kwargs):
  return [{'state':'no_safe_candidates','examined':2,'short_page_scan':True,
           'next_scan_after_seconds':60}]
 monkeypatch.setattr(cleanup,'tick',ineligible)
 monkeypatch.setattr(cleanup.asyncio,'sleep',sleep)
 with pytest.raises(asyncio.CancelledError):await cleanup.run(db)
 assert delays==[60]


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
async def test_remote_failure_retries_same_candidate_page_after_backoff(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 (tmp_path/'draft-cleanup.json').write_text(json.dumps({'enabled':True,'paged_scan':True}))
 with db.connect() as c:c.execute('INSERT OR REPLACE INTO draft_cleanup_scan_cursors VALUES(?,?)',(owner,7))
 def page(db,owner,settings):
  with db.connect() as c:c.execute('INSERT OR REPLACE INTO draft_cleanup_scan_cursors VALUES(?,?)',(owner,12))
  return {'account':[item]}
 monkeypatch.setattr(cleanup,'candidates',page)
 async def timeout(*args,**kwargs):raise TimeoutError()
 monkeypatch.setattr(cleanup,'clean_account',timeout)
 assert (await cleanup.tick(db))[0]['state']=='error'
 with db.connect() as c:
  assert c.execute('SELECT last_rowid FROM draft_cleanup_scan_cursors WHERE owner=?',(owner,)).fetchone()[0]==7
  assert c.execute('SELECT failures FROM draft_cleanup_backoff WHERE account="account"').fetchone()[0]==1
  c.execute('UPDATE draft_cleanup_backoff SET due=0 WHERE account="account"')
 async def empty(*args,**kwargs):return {'state':'no_safe_candidates','deleted':0,'examined':0}
 monkeypatch.setattr(cleanup,'clean_account',empty)
 assert (await cleanup.tick(db))[0]['state']=='no_safe_candidates'
 with db.connect() as c:
  assert c.execute('SELECT last_rowid FROM draft_cleanup_scan_cursors WHERE owner=?',(owner,)).fetchone()[0]==12


@pytest.mark.asyncio
async def test_backoff_write_wait_does_not_block_event_loop(tmp_path,monkeypatch):
 db,owner,item=setup(tmp_path)
 monkeypatch.setattr(cleanup,'candidates',lambda *args:{'account':[item]})
 entered=threading.Event();release=threading.Event();holder=[]
 def hold_writer():
  with db.connect() as c:
   c.execute('BEGIN IMMEDIATE')
   entered.set()
   release.wait(2)
 async def no_candidates(*args,**kwargs):
  holder.append(asyncio.create_task(asyncio.to_thread(hold_writer)))
  await asyncio.to_thread(entered.wait)
  return {'state':'no_safe_candidates','deleted':0,'examined':1}
 monkeypatch.setattr(cleanup,'clean_account',no_candidates)
 task=asyncio.create_task(cleanup.tick(db))
 try:
  await asyncio.wait_for(asyncio.to_thread(entered.wait),1)
  async def heartbeat():
   for _ in range(5):await asyncio.sleep(.02)
  await asyncio.wait_for(heartbeat(),.3)
  assert not task.done()
 finally:
  release.set()
  await asyncio.gather(*holder)
 result=await asyncio.wait_for(task,2)
 assert result[0]['next_scan_after_seconds']==600
 with db.connect() as c:
  assert c.execute('SELECT failures FROM draft_cleanup_backoff WHERE account="account"').fetchone()[0]==1


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
