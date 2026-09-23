import sqlite3
import pytest
from flowhub.pipeline_modules.favorite_release import evaluate,source_guard,schema,draft_retained
from flowhub.modules import Pending


def sample():
    p=dict(owner='o',sku='1',seller='s',phase='stock_verified',verified=True,offer_id='offer',store_id='store')
    q=dict(owner='o',sku='1',seller='s',state='selling',offer_id='offer')
    return [p],[q],dict(state='ready',favorite_id='7',draft_id='8',detail={'skus':[{}]})


def test_complete_consumers_only():
    p,q,s=sample()
    assert evaluate(p,q,source=s,favorite_id='7')=='eligible_for_remote_proof'
    p.append(p[0]|{'seller':'other','phase':'submitting'})
    assert evaluate(p,q,source=s,favorite_id='7')=='publication_not_complete'


@pytest.mark.parametrize('field',['jobs','acquisition','leases','draft_receipts','favorite_receipts'])
def test_every_unresolved_consumer_blocks(field):
    p,q,s=sample()
    assert evaluate(p,q,source=s,favorite_id='7',**{field:[1]})!='eligible_for_remote_proof'


def test_identity_and_incomplete_source_block():
    p,q,s=sample()
    assert evaluate(p,q,source=s,favorite_id='99')=='favorite_identity'
    assert evaluate(p,q,source=s|{'detail':{}},favorite_id='7')=='incomplete_snapshot'
    assert evaluate(p,[],source=s,favorite_id='7')=='missing_consumers'


def test_new_dependency_cannot_overlap_deletion_but_other_sku_runs(tmp_path):
    with source_guard(tmp_path,'1'):
        with pytest.raises(Pending):
            with source_guard(tmp_path,'1'):pass
        with source_guard(tmp_path,'2'):pass
    with source_guard(tmp_path,'1'):pass


def test_draft_pin_survives_unknown_and_deleted(tmp_path):
    c=sqlite3.connect(tmp_path/'db');schema(c)
    for state in ['intent','uncertain','deleted']:
        c.execute('INSERT OR REPLACE INTO favorite_release_certificates VALUES(?,?,?,?,?,?,?,?)',('a','7','1','key','8',state,'{}',0))
        assert draft_retained(c,'8')
    assert not draft_retained(c,'9')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['ok','lost_ack','unknown_present','bad_draft'])
async def test_execute_retains_draft_and_never_replays(tmp_path,mode):
    import json
    from flowhub.db import Database
    from flowhub.pipeline_modules.favorite_release import execute_one
    db=Database(tmp_path/'data');p,q,s=sample();s['source_key']='1'
    with db.connect() as c:
        c.execute('CREATE TABLE plugin_publications(owner,sku,seller,body,updated)')
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?,?)',('o','1','s',json.dumps(p[0]),0))
        c.execute('CREATE TABLE plugin_pipeline(owner,sku,seller,state,body)')
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?)',('o','1','s','selling',json.dumps({'offer_id':'offer'})))
        c.execute('CREATE TABLE plugin_pipeline_leases(owner,sku,seller,token,expires)')
        c.execute('CREATE TABLE source_details(key,state,body,updated)')
        c.execute('INSERT INTO source_details VALUES(?,?,?,?)',('source','ready',db.seal(s),1))
        c.execute('CREATE TABLE draft_cleanup_receipts(draft_id,state)')
    class Fake:
        present=True;writes=0
        async def call(self,path,method='GET',params=None,body=None):
            if path.endswith('favorite/lists'):
                return {'total':int(self.present),'data':[{'id':7,'sku':'1'}] if self.present else []}
            if path.endswith('collect/detail'):return {} if mode=='bad_draft' else {'skus':[{}]}
            if path.endswith('online/lists'):return {'data':[{'shop_id':'shop','offer_id':'offer','online_status':'selling','stock':99}]}
            assert path.endswith('favorite/toggle') and method=='POST' and body['status'] is False
            with db.connect() as c:assert draft_retained(c,'8')
            self.writes+=1
            if mode!='unknown_present':self.present=False
            if mode in ('lost_ack','unknown_present'):raise TimeoutError()
            return {}
    api=Fake();item={'account':'a','sku':'1','favorite_id':'7','source_key':'source','business_revision':'v1'}
    args=(db,item,api,{'store':{'client':api,'shop_id':'shop'}},tmp_path/'archive')
    r=await execute_one(*args)
    assert r['state']==({'ok':'deleted','lost_ack':'deleted','unknown_present':'uncertain','bad_draft':'protected'}[mode])
    await execute_one(*args)
    assert api.writes==(0 if mode=='bad_draft' else 1)
