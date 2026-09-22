import asyncio
import json
import time
from flowhub.pipeline_modules.repair_queue import can_expand,tick_repair
from flowhub.pipeline_modules import control
from flowhub.db import Database


def test_second_worker_uses_repair_health_and_backs_off_on_failure(tmp_path):
    db=Database(tmp_path);control.schema(db);now=time.time()
    assert not can_expand(db,now)
    with db.connect() as c:
        for i in range(2):
            c.execute('INSERT INTO pipeline_module_events VALUES(NULL,?,?,?,?,?,?,?)',
                      ('seed','o',str(i),now-70+i,now-10+i,'publishing',json.dumps({'lane':'seed_repair','reason':None})))
    assert can_expand(db,now)
    with db.connect() as c:
        c.execute('INSERT INTO pipeline_module_events VALUES(NULL,?,?,?,?,?,?,?)',
                  ('seed','o','3',now-60,now,'needs_fields',json.dumps({'lane':'seed_repair','reason':'timeout'})))
    assert not can_expand(db,now)
    assert not can_expand(db,now+301)


async def test_two_repairs_choose_separate_queues_with_existing_leases(tmp_path,monkeypatch):
    from test_modular_pipeline import setup
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db,_=setup(tmp_path,2)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='needs_fields'")
        c.execute("UPDATE plugin_pipeline SET body=? WHERE sku='1'",(json.dumps({'official_dossier_pending':True}),))
    seen=[];started=asyncio.Event();release=asyncio.Event()
    async def repair(self,db,owner,sku,seller):
        seen.append(sku)
        if len(seen)==2:started.set()
        await release.wait()
        return {'state':'waiting','reason':'missing:attributes'}
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    tasks=[asyncio.create_task(tick_repair(db,i,pipeline.tick)) for i in range(2)]
    await asyncio.wait_for(started.wait(),2)
    assert set(seen)=={'1','2'}
    assert not await pipeline.tick(db,lane='seed_repair')
    release.set();assert all(await asyncio.gather(*tasks))


def recovery_fixture():
    cb={'search_and_rank':{'query':{'product_id':'1','title':'Товар','image_url':'https://example.com/a.jpg','specifications':{'attributes':[],'variant_id':'v'}},
        'candidates':[{'candidate':{'offer_id':'9','image_url':'https://example.com/supplier.jpg'}}]},
        'decision':{'selected_offer_id':'9','qwen_review':{}}}
    review={'state':'needs_review','finished_at':900,'candidate':{'source_key':'1','title':'Товар','image':'https://example.com/a.jpg',
        'origin':{'seller_id':'2','category_id':'10','plugin_detail':{'variant_id':'v','attributes':[]}}},
        'result':{'evidence':{'source':{'comparebot':cb},'profit':{'input':{'package_length':10,'package_width':10,'package_height':10,'package_weight':100}}}}}
    receipt={'id':7,'at':1000,'action':'approve','actor':'reviewer','note':'confirmed','body':json.dumps({'review':json.dumps(review)})}
    p={'sku':'1','seller_id':'2','title':'Товар','image':'https://example.com/a.jpg','category_id':'10',
       'plugin_detail':{'variant_id':'v','attributes':[{'id':1,'values':['new']}],'dimensions_mm':[100,100,100],'weight_g':100}}
    return receipt,p,{}


def test_lost_human_approval_can_be_restored_from_receipt_without_new_approval_time():
    from flowhub.pipeline_modules.repair_recovery import restored_identity
    receipt,p,current=recovery_fixture()
    r=restored_identity(receipt,p,current)
    assert r['state']=='same_product_confirmed' and r['finished_at']==900
    assert r['identity_review']['human_review']['at']==1000
    assert r['identity_review']['repair_restoration']['receipt_id']==7
    assert r['identity_review']['publication_authorized'] is False
    assert 'automatic_review' not in r['identity_review']


import pytest
@pytest.mark.parametrize('change',['title','image','seller','variant','package','category','known_attribute','conflict','rejected'])
def test_receipt_recovery_refuses_material_changes_and_later_rejections(change):
    from flowhub.pipeline_modules.repair_recovery import restored_identity
    receipt,p,current=recovery_fixture()
    if change=='title':p['title']='Another'
    if change=='image':p['image']='https://example.com/other.jpg'
    if change=='seller':p['seller_id']='3'
    if change=='variant':p['plugin_detail']['variant_id']='other'
    if change=='package':p['plugin_detail']['weight_g']=200
    if change=='category':p['category_id']='11'
    if change=='known_attribute':
        snap=json.loads(receipt['body']);r=json.loads(snap['review'])
        r['candidate']['origin']['plugin_detail']['attributes']=[{'id':1,'values':['old']}]
        receipt['body']=json.dumps({'review':json.dumps(r)})
    if change=='conflict':current={'identity_review':{'comparison':{'decision':{'qwen_review':{'brand_or_model_conflict':True}}}}}
    if change=='rejected':receipt['action']='reject'
    with pytest.raises(ValueError):restored_identity(receipt,p,current)
