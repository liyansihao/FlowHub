import json,time
import pytest
from flowhub.evaluation_requirements import review_sale_price as sale_price
from flowhub.identity_review import verdict,confirm,envelope
from test_manual_reviews import setup,rev
from flowhub.manual_reviews import decide


def product():
 return {'sku':'123','seller_id':'456','title':'clip','image':'https://example.com/clip.jpg','collected_at':1,
         'current_price_display':'23,42 ¥','source_relation':{'seller_id':'456','root_seeds':[{'sku':'1'}]}}


def test_available_ordinary_price_keeps_historical_timestamp():
 q=sale_price(product(),now=99999)
 assert q['value']==23.42 and q['observed_at']==1 and q['historical']


def test_minimum_follow_precedes_ordinary_and_average_is_not_asking_price():
 p=product();p['minimum_follow_price']={'value':110,'currency':'RUB','observed_at':2}
 assert sale_price(p,now=99999)['value']==110
 p={'plugin_detail':{'monthly_sales':{'average_price_rub':200,'observed_at':time.time()}}}
 assert sale_price(p) is None


def cb(p,verdict='match',score=.9):
 c=envelope(p)
 return {'search_and_rank':{'query':{'product_id':'123','title':c['title'],'image_url':c['image'],'specifications':c['origin']['specifications']},'candidates':[{'candidate':{'offer_id':'789','image_url':'https://example.com/supplier.jpg'},'dinov2_similarity':score}]},'decision':{'selected_offer_id':'789','outcome':'approved','qwen_review':{'verdict':verdict}}}


def test_same_product_confirmation_does_not_publish_or_require_profit(tmp_path):
 db,o=setup(tmp_path);p=product()
 with db.connect() as c:
  from flowhub.source_library import SourceLibrary
  SourceLibrary(db).put(o,p,{'channel':'test'},connection=c)
  c.execute('UPDATE plugin_pipeline SET body=?',(json.dumps({'same_product_only':True,'repair_retry':{'exhausted_at':1}}),))
  r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0]);r['finished_at']=1
  r['result']['evidence'].pop('profit',None)
  r['identity_review']={'comparison':cb(p),'verdict':'uncertain'}
  c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
 assert decide(db,o,'user','123','456','approve','图片规格一致',rev(db,o))['state']=='same_product_confirmed'
 with db.connect() as c:
  r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0])
  assert r['identity_review']['publication_authorized'] is False
  assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]!='publishing'


def test_low_confidence_and_conflict_are_not_auto_same_product():
 p=product();assert verdict(cb(p,score=.7))=='uncertain'
 b=cb(p);b['decision']['qwen_review']['brand_or_model_conflict']=True
 assert verdict(b)=='mismatch'
 with pytest.raises(ValueError):confirm({'identity_review':{'comparison':b}},'user','same')


@pytest.mark.asyncio
async def test_identity_evaluation_uses_image_review_without_profit(tmp_path,monkeypatch):
 from flowhub.identity_review import evaluate
 from flowhub.source_library import SourceLibrary
 import flowhub.identity_review as module
 db,o=setup(tmp_path);p=product()
 with db.connect() as c:
  SourceLibrary(db).put(o,p,{'channel':'test'},connection=c)
  c.execute("UPDATE workflows SET modules=?,secrets=? WHERE owner=?",(json.dumps({'matcher':'flowb-matcher'}),db.seal({'flowb-matcher':json.dumps({'erp_token':'test','dashscope_api_key':'test'})}),o))
  c.execute('UPDATE plugin_reviews SET body=?',(json.dumps({'state':'error'}),))
 calls=[]
 async def screen(candidate,*args,**kwargs):
  assert not candidate.get('price') and not candidate.get('weight_g')
  calls.append(kwargs);return cb(p)
 monkeypatch.setattr(module,'screen',screen)
 result=await evaluate(db,o,'123','456')
 assert len(calls)==2 and result['identity_review']['verdict']=='match'
 assert result['state']=='same_product_confirmed'
 with db.connect() as c:assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='needs_review'


@pytest.mark.asyncio
async def test_empty_supplier_search_is_not_a_false_mismatch(monkeypatch):
 from flowhub.comparebot import screen
 import flowhub.comparebot as module
 monkeypatch.delenv('FLOWHUB_COMPAREBOT_WARM',raising=False)
 import flowhub.cluster_compute as cluster
 async def no_remote(*args,**kwargs):return None
 monkeypatch.setattr(cluster,'remote',no_remote)
 class Process:
  returncode=1
  async def wait(self):return 1
 async def start(*args,**kwargs):
  kwargs['stderr'].write(b'RuntimeError: 1688 image search returned no candidates\n');kwargs['stderr'].flush()
  return Process()
 monkeypatch.setattr(module.asyncio,'create_subprocess_exec',start)
 cb_result=await screen(envelope(product()))
 assert verdict(cb_result)=='no_candidates'
 assert cb_result['decision']['outcome']=='manual_review'


@pytest.mark.parametrize('conflict',[False,True])
def test_website_scope_matches_identity_card_for_legacy_queue(tmp_path,conflict):
 from flowhub.source_library import SourceLibrary
 from flowhub.listing_controls import management_card
 from flowhub.manual_reviews import load
 db,o=setup(tmp_path);p=product()
 with db.connect() as c:
  SourceLibrary(db).put(o,p,{'channel':'test'},connection=c)
  r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0])
  r['result'].update(manual_review=False,reason='old_profit_rule')
  r['identity_review']={'comparison':cb(p),'verdict':'uncertain'}
  if conflict:r['identity_review']['comparison']['decision']['qwen_review']['brand_or_model_conflict']=True
  c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
  card=management_card(c,o,load(c,o,'123','456'),'123','456')
 v=rev(db,o)
 if conflict:
  assert not card['can_approve']
  with pytest.raises(ValueError,match='冲突'):decide(db,o,'user','123','456','approve','用户确认',v,review_scope='same_product_only')
 else:
  assert card['can_approve']
  out=decide(db,o,'user','123','456','approve','用户确认',v,review_scope='same_product_only')
  assert out['state']=='same_product_confirmed'
  assert decide(db,o,'user','123','456','approve','用户确认',v,replay=True,review_scope='same_product_only')['replayed']
  with db.connect() as c:
   assert c.execute('SELECT action FROM product_listing_controls').fetchone()[0]=='list'
   assert c.execute('SELECT count(*) FROM human_reviews').fetchone()[0]==1


@pytest.mark.parametrize('score,size,qwen,expected',[
 (.6299,'small',None,'mismatch'),(.63,'small',None,'uncertain'),
 (.8599,'small',None,'uncertain'),(.86,'small',None,'match'),
 (.92,'unknown',None,'uncertain'),(.92,'large',None,'uncertain'),
 (.8199,'large','match','uncertain'),(.82,'large','match','match'),
 (.64,'large','mismatch','mismatch'),(.6401,'large','mismatch','uncertain'),
 (.92,'large','mismatch','uncertain'),(.85,'small','uncertain','uncertain'),
 (float('nan'),'small','match','uncertain'),(float('inf'),'small','match','uncertain'),
])
def test_identity_policy_boundaries(score,size,qwen,expected):
 b=cb(product(),score=score)
 b['search_and_rank']['query']['size']=size
 b['decision']['outcome']='manual_review'
 b['decision']['qwen_review']={'verdict':qwen} if qwen else None
 assert verdict(b)==expected


def test_identity_envelope_preserves_size_facts_without_requiring_them():
 from flowhub.comparebot import manifest
 p=product();p['plugin_detail']={'weight_g':200}
 assert manifest(envelope(p))['size']=='small'
 p['plugin_detail']['weight_g']=500
 assert manifest(envelope(p))['size']=='large'
 p.pop('plugin_detail')
 assert manifest(envelope(p))['size']=='unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('score,size,expected',[(.92,'small','same_product_confirmed'),(.62,'unknown','rejected')])
async def test_cached_dino_decisions_leave_manual_queue_without_qwen(tmp_path,monkeypatch,score,size,expected):
 from flowhub.source_library import SourceLibrary
 from flowhub import plugin_pipeline
 import flowhub.identity_review as module
 db,o=setup(tmp_path);p=product();b=cb(p,score=score)
 b['search_and_rank']['query']['size']=size
 b['decision']['qwen_review']=None
 b['decision']['outcome']='manual_review'
 with db.connect() as c:
  SourceLibrary(db).put(o,p,{'channel':'test'},connection=c)
  c.execute("UPDATE plugin_pipeline SET state='queued',body=?",(json.dumps({'same_product_only':True}),))
  c.execute('UPDATE plugin_reviews SET body=?',(json.dumps({'result':{'evidence':{'comparebot':b}}}),))
 async def forbidden(*args,**kwargs):raise AssertionError('cached DINO decision needs no network')
 monkeypatch.setattr(module,'screen',forbidden)
 assert await plugin_pipeline.tick(db,lane='review')
 with db.connect() as c:
  assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]==expected
  r=c.execute('SELECT state,body FROM plugin_reviews').fetchone()
  assert r['state']==expected
  assert json.loads(r['body'])['identity_review']['publication_authorized'] is False
  assert c.execute('SELECT count(*) FROM product_listing_controls').fetchone()[0]==0


@pytest.mark.parametrize('guard',[None,'human','changed','active','blocked'])
def test_policy_migration_is_idempotent_and_preserves_manual_work(tmp_path,guard):
 from scripts.apply_identity_policy import migrate
 from flowhub.source_library import SourceLibrary
 db,o=setup(tmp_path);p=product();b=cb(p)
 b['search_and_rank']['query']['size']='small';b['decision']['qwen_review']=None
 if guard=='changed':b['search_and_rank']['query']['title']='different'
 with db.connect() as c:
  SourceLibrary(db).put(o,p,{'channel':'test'},connection=c)
  r={'result':{'evidence':{'comparebot':b}}}
  if guard=='human':r['identity_review']={'human_review':{'actor':'user'}}
  if guard=='active':c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(o,'123','456','test',time.time()+60))
  if guard=='blocked':c.execute('INSERT INTO blocks VALUES(?,?,?)',(o,'123','test'))
  c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
 stats=migrate(db,apply=True,backup_path=tmp_path/'backup.json')
 with db.connect() as c:
  assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]==('needs_review' if guard else 'same_product_confirmed')
  assert c.execute('SELECT count(*) FROM product_listing_controls').fetchone()[0]==0
 if not guard:
  assert stats['same_product_confirmed']==1
  assert json.loads((tmp_path/'backup.json').read_text())[0]['before']['state']=='needs_review'
  assert migrate(db,apply=True,backup_path=tmp_path/'second.json')=={}


@pytest.mark.asyncio
async def test_new_legacy_review_does_not_strand_approved_dino(tmp_path,monkeypatch):
 from flowhub.source_library import SourceLibrary
 from flowhub import plugin_pipeline
 db,o=setup(tmp_path);p=product();b=cb(p)
 b['search_and_rank']['query']['size']='small';b['decision']['qwen_review']=None
 with db.connect() as c:
  SourceLibrary(db).put(o,p,{'channel':'test'},connection=c)
  c.execute("UPDATE plugin_pipeline SET state='queued'")
  c.execute('UPDATE plugin_reviews SET body=?',(json.dumps({'result':{'evidence':{'comparebot':b}}}),))
 async def evaluate(*args):return {'state':'needs_review','reason':'old_profit_rule'}
 monkeypatch.setattr(plugin_pipeline,'evaluate',evaluate)
 assert await plugin_pipeline.tick(db,lane='review')
 with db.connect() as c:
  assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='same_product_confirmed'
  assert c.execute('SELECT count(*) FROM product_listing_controls').fetchone()[0]==0
