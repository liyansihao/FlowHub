import json,time
import pytest
from flowhub.pipeline_modules.direct_facts import merge_fields,page_fields,public_detail
from flowhub.pipeline_modules.repair import merge_draft


def test_only_package_measurements_and_exact_sku_are_accepted():
 p={'sku':'123'}
 row={'@type':'Product','sku':'123','additionalProperty':[{'name':'Вес с упаковкой, г','value':'125'},{'name':'Размеры в упаковке, см','value':'10 x 20 x 3'},{'name':'Вес товара, г','value':'999'}]}
 html='<script type="application/ld+json">'+json.dumps(row)+'</script>'
 product,step=page_fields(p,html,time.time())
 assert product['plugin_detail']['weight_g']==125
 assert product['plugin_detail']['dimensions_mm']==[100,200,30]
 assert 'attributes' not in product['plugin_detail'] and 'observed_at' not in product['plugin_detail']
 wrong,step=page_fields({'sku':'999'},html,time.time())
 assert step['reason']=='exact_product_unavailable' and wrong=={'sku':'999'}


def test_partial_fields_do_not_refresh_old_unread_attributes():
 now=time.time();p={'sku':'123','plugin_detail':{'attributes':[{'id':1}],'observed_at':now-22000}}
 p,_=merge_fields(p,{'weight_g':20,'dimensions_mm':[10,20,30]},'direct',now)
 assert p['plugin_detail']['observed_at']==now-22000
 assert p['plugin_detail']['field_observations']['attributes']['observed_at']==now-22000


def test_draft_only_fills_missing_fields_and_preserves_fresh_direct_values():
 now=time.time();p={'sku':'123','plugin_detail':{'weight_g':20,'observed_at':now,'field_observations':{'weight_g':{'source':'direct','observed_at':now}}}}
 result=merge_draft(p,{'source_key':'123','draft_id':9,'observed_at':now+1,'detail':{'skus':[{}],'package_weight':999,'package_length':10,'package_width':20,'package_height':30,'common_attributes':[{'id':1}]}})
 d=result['plugin_detail'];assert d['weight_g']==20 and d['dimensions_mm']==[10,20,30]
 assert d['observed_at']==now and d['field_observations']['weight_g']['source']=='direct'


@pytest.mark.asyncio
async def test_direct_complete_skips_draft_and_incomplete_falls_back(tmp_path,monkeypatch):
 from test_maozi_field_repair import setup
 from flowhub.pipeline_modules.repair import PriceRepairModule
 from flowhub.pipeline_modules import direct_facts
 from flowhub.maozi import MaoziPublisher
 from flowhub.source_detail import SourceCollector
 db,owner=setup(tmp_path)
 (tmp_path/'direct-first.json').write_text('{"enabled":true}')
 with db.connect() as c:
  r=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0]);r['plugin_detail']={};r['proposed_sale_price']={'value':20,'currency':'CNY','observed_at':time.time()};c.execute('UPDATE sourcing_products SET body=?',(json.dumps(r),))
 calls=[]
 async def erp(*args,**kwargs):calls.append('direct');return {'data':{'sku':'1'},'status':{'update_sales':True}}
 async def page(db,p):
  calls.append('page');p,fields=merge_fields(p,{'weight_g':20,'dimensions_mm':[10,20,30],'attributes':[{'id':1}]},'verified-direct-test',time.time());return p,{'fields':fields}
 async def draft(*args):pytest.fail('complete direct dossier must not request a draft')
 monkeypatch.setattr(MaoziPublisher,'erp',erp);monkeypatch.setattr(direct_facts,'public_detail',page);monkeypatch.setattr(SourceCollector,'collect',draft)
 assert (await PriceRepairModule().run(db,owner,'1','2'))['state']=='ready'
 assert calls==['direct','page']
 # Re-running complete facts does not issue either direct requests or draft writes.
 calls.clear();assert (await PriceRepairModule().run(db,owner,'1','2'))['state']=='ready';assert not calls
 # Missing dimensions fall back to a draft; other valid direct fields stay unchanged.
 with db.connect() as c:
  r=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0]);r['plugin_detail'].pop('dimensions_mm');c.execute('UPDATE sourcing_products SET body=?',(json.dumps(r),))
 async def unavailable(db,p):calls.append('page');return p,{'reason':'http_403','fields':[]}
 async def supplement(*args):
  calls.append('draft');return {'source_key':'1','draft_id':9,'observed_at':time.time(),'detail':{'skus':[{}],'package_weight':999,'package_length':10,'package_width':20,'package_height':30,'common_attributes':[{'id':999}]}}
 monkeypatch.setattr(direct_facts,'public_detail',unavailable);monkeypatch.setattr(SourceCollector,'collect',supplement)
 assert (await PriceRepairModule().run(db,owner,'1','2'))['state']=='ready'
 assert calls==['direct','page','draft']
 with db.connect() as c:
  r=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
  assert r['plugin_detail']['weight_g']==20 and r['plugin_detail']['attributes']==[{'id':1}]


@pytest.mark.asyncio
async def test_same_origin_redirect_can_read_details_without_browser(tmp_path,monkeypatch):
 import httpx
 from flowhub.db import Database
 from flowhub.pipeline_modules import direct_facts
 original=httpx.AsyncClient;seen=[]
 def handle(request):
  seen.append(str(request.url))
  if not request.url.query:return httpx.Response(307,headers={'location':str(request.url)+'?__rr=1'})
  row={'@type':'Product','sku':'123','additionalProperty':[{'name':'Вес с упаковкой, г','value':'125'}]}
  return httpx.Response(200,text='<script type="application/ld+json">'+json.dumps(row)+'</script>')
 monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(**kw,transport=httpx.MockTransport(handle)))
 p,step=await direct_facts.public_detail(Database(tmp_path),{'sku':'123'})
 assert p['plugin_detail']['weight_g']==125 and len(seen)==2
 assert step['body_sha256']


@pytest.mark.asyncio
@pytest.mark.parametrize('exact',[True,False])
async def test_session_rendered_identity_decides_after_initial_403(tmp_path,monkeypatch,exact):
 from flowhub.db import Database
 from flowhub.pipeline_modules import direct_facts
 db=Database(tmp_path);(tmp_path/'direct-first.json').write_text(json.dumps({'browser_session':{'enabled':True,'session':'existing'}}))
 # A bare HTTP backoff must not suppress the authorized existing session.
 (tmp_path/'public-detail-backoff.json').write_text(json.dumps({'until':time.time()+900}))
 row={'@type':'Product','sku':'123' if exact else '999','name':'title'}
 html='<script type="application/ld+json">'+json.dumps(row)+'</script>'
 class Process:
  returncode=0
  async def communicate(self,data):
   assert json.loads(data)['sku']=='123'
   return json.dumps({'initial_status':403,'html':html,'url':'https://www.ozon.ru/product/123/'}).encode(),b''
 async def spawn(*args,**kwargs):return Process()
 monkeypatch.setattr(direct_facts.asyncio,'create_subprocess_exec',spawn)
 p,step=await direct_facts.public_detail(db,{'sku':'123'})
 if exact:
  assert p['title']=='title' and step['initial_http_status']==403 and step['reason']=='exact_product_read'
 else:
  assert p=={'sku':'123'} and (tmp_path/'session-detail-backoff.json').exists()


def test_category_read_binds_exact_sku_and_converts_cm_without_inventing_attributes():
 from flowhub.pipeline_modules.direct_facts import from_category
 p={'sku':'123'};response={'sku':'123','cate':[1,2,3],'product_info':{'weight':40,'depth':25,'width':25,'height':5}}
 updated,step=from_category(p,response,100)
 d=updated['plugin_detail']
 assert d['sku']=='123' and d['weight_g']==40 and d['dimensions_mm']==[250,250,50]
 assert 'attributes' not in d and 'observed_at' not in d
 assert p=={'sku':'123'}
 assert from_category({'sku':'999'},response,100)[1]['reason']=='sku_mismatch'
 empty,step=from_category(p,{'sku':'123','product_info':{}},100)
 assert not step['fields'] and not empty['plugin_detail'].get('weight_g')
