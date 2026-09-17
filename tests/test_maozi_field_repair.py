import json,time
import pytest
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.pipeline_modules.repair import PriceRepairModule,merge_draft,missing_fields
from flowhub.maozi import MaoziPublisher


def setup(tmp_path):
    db=Database(tmp_path);library=SourceLibrary(db)
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('CREATE TABLE plugin_routes(owner TEXT,sku TEXT,seller TEXT,store_id TEXT,expires REAL,run_id TEXT)')
        c.execute('CREATE TABLE plugin_reviews(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
        c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(owner,'1','2','test',0,'expired-publication'))
        c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',('test',owner,'test','maozi','{}',db.seal({'erp_token':'test'})))
    p={'sku':'1','seller_id':'2','title':'x','image':'https://example.com/a.jpg','url':'https://www.ozon.ru/product/1/','collected_at':0,
       'plugin_detail':{'sku':'1','observed_at':time.time(),'weight_g':20,'dimensions_mm':[100,100,10],'attributes':[{'id':1}],
                        'monthly_sales':{'blocked_by_seller':True}}}
    library.put(owner,p,{'channel':'test'})
    return db,owner


@pytest.mark.asyncio
async def test_expired_publication_does_not_block_readonly_erp_supplement(tmp_path,monkeypatch):
    db,owner=setup(tmp_path);calls=[]
    async def erp(self,method,path,**kwargs):
        calls.append((method,path,kwargs));return {'status':{'update_sales':False},'data':{'sku':'1','sellerId':'2','avgPrice':300,'blockedBySeller':None}}
    monkeypatch.setattr(MaoziPublisher,'erp',erp)
    result=await PriceRepairModule().run(db,owner,'1','2')
    assert result['state']=='ready' and len(calls)==1
    assert calls[0][2]['params']=={'sku':'1'}
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        assert p['plugin_detail']['monthly_sales']['average_price_rub']==300
        assert p['plugin_detail']['monthly_sales']['blocked_by_seller'] is True
        assert c.execute('SELECT count(*) FROM plugin_repair_events').fetchone()[0]==1
        assert c.execute('SELECT expires FROM plugin_routes').fetchone()[0]==0


@pytest.mark.asyncio
async def test_pending_or_wrong_seller_never_becomes_price_evidence(tmp_path,monkeypatch):
    db,owner=setup(tmp_path)
    async def erp(self,*args,**kwargs):return {'status':{'update_sales':True},'data':{'sku':'1','sellerId':'2','avgPrice':300}}
    monkeypatch.setattr(MaoziPublisher,'erp',erp)
    r=await PriceRepairModule().run(db,owner,'1','2');assert r['missing_fields']==['sale_price']
    async def wrong(self,*args,**kwargs):return {'status':{'update_sales':False},'data':{'sku':'1','sellerId':'999','avgPrice':300}}
    monkeypatch.setattr(MaoziPublisher,'erp',wrong)
    r=await PriceRepairModule().run(db,owner,'1','2');assert r['missing_fields']==['sale_price']


def test_draft_supplies_physical_fields_without_inventing_price():
    p={'sku':'1','seller_id':'2','title':'x','url':'https://www.ozon.ru/product/1/','image':'https://example.com/a.jpg'}
    packet={'source_key':'1','observed_at':time.time(),'draft_id':5,'detail':{'skus':[{}],'package_weight':25,'package_length':100,'package_width':80,'package_height':10,'common_attributes':[{'id':1}],'category_id':[1,2,3]}}
    result=merge_draft(p,packet)
    assert result['plugin_detail']['weight_g']==25
    assert missing_fields(result)==['sale_price']
    with pytest.raises(ValueError):merge_draft(p,packet|{'source_key':'9'})


@pytest.mark.asyncio
@pytest.mark.parametrize('currency,value,expected', [('CNY',30,30),('RUB',300,27)])
async def test_unreviewed_product_can_bootstrap_physical_draft(tmp_path,monkeypatch,currency,value,expected):
    from flowhub.source_detail import SourceCollector
    db,owner=setup(tmp_path)
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p['plugin_detail']={}
        p['proposed_sale_price']={'value':value,'currency':currency,'observed_at':time.time()}
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
    async def erp(self,method,path,**kwargs):
        assert path=='/api.exchange_rate/index'
        return {'RUBCNY':{'value':0.09}}
    async def collect(self):
        assert self.c['candidate']['price']==expected
        assert not self.c['existing_favorite_only']
        return {'source_key':'1','observed_at':time.time(),'draft_id':5,'detail':{'skus':[{}],'package_weight':25,'package_length':100,'package_width':80,'package_height':10,'common_attributes':[{'id':1}],'category_id':[]}}
    monkeypatch.setattr(MaoziPublisher,'erp',erp)
    monkeypatch.setattr(SourceCollector,'collect',collect)
    result=await PriceRepairModule().run(db,owner,'1','2')
    assert result['state']=='ready'


@pytest.mark.asyncio
async def test_price_failure_preserves_independent_physical_progress_and_audit(tmp_path,monkeypatch):
    from flowhub.source_detail import SourceCollector
    db,owner=setup(tmp_path)
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0]);p['plugin_detail']={}
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
    async def erp(self,*args,**kwargs):raise TimeoutError('sensitive request detail')
    async def collect(self):
        assert self.c['candidate']['price'] is None and self.c['existing_favorite_only']
        return {'source_key':'1','observed_at':time.time(),'draft_id':5,'detail':{'skus':[{}],'package_weight':25,'package_length':100,'package_width':80,'package_height':10,'common_attributes':[{'id':1}]}}
    monkeypatch.setattr(MaoziPublisher,'erp',erp);monkeypatch.setattr(SourceCollector,'collect',collect)
    result=await PriceRepairModule().run(db,owner,'1','2')
    assert result['missing_fields']==['sale_price']
    with db.connect() as c:
        event=c.execute('SELECT body FROM plugin_repair_events').fetchone()[0]
        assert 'TimeoutError' in event and 'sensitive' not in event
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        assert p['plugin_detail']['weight_g']==25


@pytest.mark.asyncio
async def test_complete_but_expired_dossier_is_refetched_before_recalculation(tmp_path,monkeypatch):
    from flowhub.source_detail import SourceCollector
    db,owner=setup(tmp_path)
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p['plugin_detail']['observed_at']=time.time()-21601
        p['proposed_sale_price']={'value':30,'currency':'CNY','observed_at':time.time()}
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
    assert missing_fields(p)==['fresh_dossier']
    calls=[]
    async def collect(self):
        calls.append(self.sku)
        return {'source_key':'1','observed_at':time.time(),'draft_id':5,'detail':{'skus':[{}],'package_weight':25,'package_length':100,'package_width':80,'package_height':10,'common_attributes':[{'id':1}]}}
    monkeypatch.setattr(SourceCollector,'collect',collect)
    result=await PriceRepairModule().run(db,owner,'1','2')
    assert result['state']=='ready' and calls==['1']


def test_partial_draft_keeps_weight_without_claiming_fresh_complete_dossier():
    from flowhub.pipeline_modules.repair import merge_draft
    p={'sku':'1','plugin_detail':{'observed_at':1}}
    result=merge_draft(p,{'source_key':'1','observed_at':123,'detail':{'skus':[{}],'package_weight':40}})
    assert result['plugin_detail']['weight_g']==40
    assert result['plugin_detail']['observed_at']==1
    assert result['plugin_detail']['field_observations']['weight_g']['observed_at']==123


@pytest.mark.asyncio
async def test_official_wrong_source_cannot_replace_identity(monkeypatch):
    from flowhub.pipeline_modules.repair import direct_identity_fields,MaoziPublisher
    async def seller(*args):return {'items':[{'sku':999,'name':'wrong','primary_image':['wrong']}]}
    monkeypatch.setattr(MaoziPublisher,'seller',seller)
    p={'sku':'1'}
    result,evidence=await direct_identity_fields(p,{'store':{'config':{},'credentials':{'client_id':'x','api_key':'y'}}})
    assert result==p and evidence['fallback']=='maozi-direct'

@pytest.mark.asyncio
async def test_category_fast_path_avoids_price_page_and_draft_requests(tmp_path,monkeypatch):
    from flowhub.pipeline_modules import direct_facts
    from flowhub.source_detail import SourceCollector
    db,owner=setup(tmp_path);(tmp_path/'direct-first.json').write_text('{"enabled":true}')
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p['plugin_detail']={};p['source_relation']={'seller_id':'2','root_seeds':[{'sku':'3','shop':'4','offer':'x'}]}
        p['proposed_sale_price']={'value':30,'currency':'CNY','observed_at':time.time()}
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
    calls=[]
    async def erp(self,method,path,**kwargs):
        calls.append(path)
        return {'sku':'1','cate':[1,2,3],'product_info':{'weight':40,'depth':25,'width':25,'height':5}}
    async def forbidden(*args):pytest.fail('fast path must not load a page or collect a draft')
    monkeypatch.setattr(MaoziPublisher,'erp',erp)
    monkeypatch.setattr(direct_facts,'public_detail',forbidden);monkeypatch.setattr(SourceCollector,'collect',forbidden)
    result=await PriceRepairModule().run(db,owner,'1','2')
    assert result['state']=='ready' and 'attributes' in result['missing_fields']
    assert calls==['/api.tool/get_category_by_sku']
    calls.clear();assert (await PriceRepairModule().run(db,owner,'1','2'))['state']=='ready'
    assert calls==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['explicit', 'manual_review', 'queue_request'])
async def test_human_repair_fetches_full_dossier_even_when_valuation_ready(tmp_path,monkeypatch,mode):
    from flowhub.source_detail import SourceCollector
    db,owner=setup(tmp_path)
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p['plugin_detail'].pop('attributes')
        p['source_relation']={'seller_id':'2','root_seeds':[{'sku':'3'}]}
        p['proposed_sale_price']={'value':30,'currency':'CNY','observed_at':time.time()}
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
        if mode=='manual_review':
            c.execute('INSERT INTO plugin_reviews VALUES(?,?,?,?)',(owner,'1','2',json.dumps({'state':'needs_review'})))
        if mode=='queue_request':
            c.execute('CREATE TABLE plugin_pipeline(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
            c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?)',(owner,'1','2','{"repair_full_dossier":true}'))
    calls=[]
    async def collect(self):
        calls.append(self.sku)
        return {'source_key':'1','observed_at':time.time(),'draft_id':5,'detail':{'skus':[{}],'package_weight':25,'package_length':100,'package_width':80,'package_height':10,'common_attributes':[{'id':1}]}}
    monkeypatch.setattr(SourceCollector,'collect',collect)
    result=await PriceRepairModule().run(db,owner,'1','2',full_dossier=mode=='explicit')
    assert calls==['1'] and result['state']=='ready' and result['missing_fields']==[]

@pytest.mark.asyncio
async def test_full_dossier_repair_cannot_claim_ready_when_attributes_still_missing(tmp_path,monkeypatch):
    from flowhub.source_detail import SourceCollector
    from flowhub.modules import Pending
    db,owner=setup(tmp_path)
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p['plugin_detail'].pop('attributes')
        p['source_relation']={'seller_id':'2','root_seeds':[{'sku':'3'}]}
        p['proposed_sale_price']={'value':30,'currency':'CNY','observed_at':time.time()}
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
    async def collect(self):raise Pending('favorite creation not confirmed; lookup again later')
    monkeypatch.setattr(SourceCollector,'collect',collect)
    result=await PriceRepairModule().run(db,owner,'1','2',full_dossier=True)
    assert result['state']=='waiting' and 'attributes' in result['missing_fields']

@pytest.mark.asyncio
async def test_old_captured_price_can_seed_acquisition_but_never_refreshes_valuation(tmp_path,monkeypatch):
    from flowhub.source_detail import SourceCollector
    db,owner=setup(tmp_path)
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p['plugin_detail']={};p['current_price_display']='25,65 ¥';p['collected_at']=1
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
    async def erp(self,*args,**kwargs):return {'status':{'update_sales':True},'data':{'sku':'1'}}
    async def collect(self):
        assert self.c['candidate']['price']==25.65 and not self.c['existing_favorite_only']
        return {'source_key':'1','observed_at':time.time(),'draft_id':5,'detail':{'skus':[{}],'package_weight':25,'package_length':100,'package_width':80,'package_height':10,'common_attributes':[{'id':1}]}}
    monkeypatch.setattr(MaoziPublisher,'erp',erp);monkeypatch.setattr(SourceCollector,'collect',collect)
    result=await PriceRepairModule().run(db,owner,'1','2',full_dossier=True)
    assert result['state']=='waiting' and result['missing_fields']==['sale_price']
    with db.connect() as c:
        saved=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
    assert saved['collected_at']==1 and not saved.get('proposed_sale_price')
    assert any(s.get('source')=='acquisition_reference_only' and s['usable_for_valuation'] is False for s in saved['collection_evidence']['last_repair']['steps'])


def test_cached_draft_refreshes_fields_stale_now_using_real_observation_time():
    now=100000
    p={'sku':'1','plugin_detail':{'observed_at':now-22000,'weight_g':25,'dimensions_mm':[100,80,10],'attributes':[{'id':1}]}}
    packet={'source_key':'1','observed_at':now-10000,'detail':{'skus':[{}],'package_weight':30,'package_length':100,'package_width':80,'package_height':10,'common_attributes':[{'id':2}]}}
    result=merge_draft(p,packet,now=now)
    assert result['plugin_detail']['observed_at']==now-10000
    assert result['plugin_detail']['weight_g']==30
    assert all(o['observed_at']==now-10000 for o in result['plugin_detail']['field_observations'].values())
    packet['observed_at']=now-23000
    unchanged=merge_draft(p,packet,now=now)
    assert unchanged['plugin_detail']['observed_at']==now-22000
    assert unchanged['plugin_detail']['weight_g']==25


async def test_official_supplement_cannot_return_ready_with_only_generic_fields(tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    from flowhub.source_detail import SourceCollector
    db,owner=setup(tmp_path)
    with db.connect() as c:
        product=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        product['proposed_sale_price']={'value':30,'currency':'CNY','observed_at':time.time()}
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(product),))
        c.execute('CREATE TABLE plugin_pipeline(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?)',(owner,'1','2',json.dumps({'repair_full_dossier':True,'official_dossier_pending':True})))
    collect=AsyncMock(return_value={'source_key':'1','observed_at':time.time(),'draft_id':5,
        'detail':{'skus':[{}],'package_weight':20,'package_length':100,'package_width':100,
                  'package_height':10,'common_attributes':[{'id':1}],'category_id':[]}})
    monkeypatch.setattr(SourceCollector,'collect',collect)
    check=AsyncMock(side_effect=lambda db,owner,p,review,context:(p,['85：必填属性缺失']))
    monkeypatch.setattr('flowhub.pipeline_modules.repair.official_dossier_fields',check)
    result=await PriceRepairModule().run(db,owner,'1','2')
    assert result['state']=='waiting' and result['missing_fields']==['85：必填属性缺失']
    collect.assert_awaited_once();check.assert_awaited_once()
