import time
import pytest
from flowhub.db import Database
from flowhub.collection_reset import clear, retry_authorized
from flowhub.collection_capacity import account
from flowhub.pipeline_modules import control
from flowhub.pipeline_modules.draft_cleanup import schema, journal
from flowhub.source_detail import SourceCollector
from flowhub.modules import Pending
from flowhub.favorite_retention import draft_retained


def setup(tmp_path):
    db=Database(tmp_path)
    for m in control.MODULES:control.set_paused(db,m,True)
    return db,{'owner':'a','candidate':{'source_key':'123'},'store':{'credentials':{'erp_token':'test'}}}


class API:
    def __init__(self,mode='ok'):
        self.present=True;self.writes=0;self.mode=mode
    async def call(self,path,method='GET',params=None):
        if path.endswith('/lists'):
            return {'total':int(self.present),'used':int(self.present),'limit':1000,
                    'data':[{'id':8,'goods_id':'123','collect_from':'ozon'}] if self.present else []}
        if path.endswith('/detail'):return {'skus':[{}],'title':'full backup'} if self.mode!='bad_backup' else {}
        assert path=='/api.product.collect/del' and method=='DELETE' and params=={'ids':'8'}
        self.writes+=1
        if self.mode=='unknown':raise TimeoutError()
        self.present=False
        if self.mode=='lost_ack':raise TimeoutError()
        return {}


def certify_retained_draft(db):
    with db.connect() as c:
        c.execute('''CREATE TABLE favorite_release_certificates(
            account TEXT, favorite_id TEXT, sku TEXT, source_key TEXT, draft_id TEXT,
            state TEXT, proof TEXT, updated REAL, PRIMARY KEY(account,favorite_id))''')
        c.execute('INSERT INTO favorite_release_certificates VALUES(?,?,?,?,?,?,?,?)',
                  ('scope','favorite','123','123','8','deleted','{}',time.time()))


@pytest.mark.asyncio
async def test_released_favorite_draft_survives_explicit_clear(tmp_path):
    db,ctx=setup(tmp_path);api=API();certify_retained_draft(db)
    with db.connect() as c:assert draft_retained(c,8)
    result=await clear(db,ctx,api,lambda _:None)
    assert result['remaining']==1 and api.writes==0
    with db.connect() as c:assert c.execute('SELECT count(*) FROM draft_cleanup_receipts').fetchone()[0]==0


@pytest.mark.asyncio
async def test_released_favorite_draft_survives_approved_retry(tmp_path):
    db,ctx=setup(tmp_path);schema(db);api=API();certify_retained_draft(db)
    journal(db,account(ctx),'8','a','123','unconfirmed',{})
    with pytest.raises(ValueError,match='retained'):
        await retry_authorized(db,ctx,['8'],'explicit-approval',api)
    assert api.writes==0
    with db.connect() as c:
        assert c.execute('SELECT state FROM draft_cleanup_receipts WHERE draft_id="8"').fetchone()[0]=='unconfirmed'
        assert c.execute('SELECT count(*) FROM collection_reset_overrides').fetchone()[0]==0


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['ok','unknown'])
async def test_explicit_retry_preserves_receipt_and_approval_cannot_repeat(tmp_path,mode):
    db,ctx=setup(tmp_path);schema(db);api=API(mode)
    journal(db,account(ctx),'8','a','123','unconfirmed',{'error_type':'RemoteProtocolError','original':'preserve'})
    result=await retry_authorized(db,ctx,['8'],'one-explicit-approval',api)
    assert result['complete']==(mode=='ok')
    with db.connect() as c:
        old=c.execute('SELECT previous_receipt FROM collection_reset_overrides').fetchone()[0]
    assert db.open(db.open(old)['body'])['original']=='preserve'
    await retry_authorized(db,ctx,['8'],'one-explicit-approval',api)
    assert api.writes==1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['ok','unknown','lost_ack'])
async def test_clear_backs_up_and_never_replays_unknown(tmp_path,mode):
    db,ctx=setup(tmp_path);api=API(mode)
    result=await clear(db,ctx,api,lambda _:None)
    assert result['complete']==(mode!='unknown')
    with db.connect() as c:r=c.execute('SELECT * FROM draft_cleanup_receipts').fetchone()
    assert db.open(r['body'])['fresh_detail']['title']=='full backup'
    await clear(db,ctx,api,lambda _:None)
    assert api.writes==1


@pytest.mark.asyncio
async def test_bad_backup_and_pause_change_prevent_deletion(tmp_path):
    db,ctx=setup(tmp_path);api=API('bad_backup')
    with pytest.raises(ValueError):await clear(db,ctx,api)
    assert api.writes==0
    api.mode='ok';control.set_paused(db,'review',False)
    with pytest.raises(RuntimeError):await clear(db,ctx,api)
    assert api.writes==0


@pytest.mark.asyncio
@pytest.mark.parametrize('deletion_state',['deleted','unconfirmed'])
async def test_expired_source_only_recreates_after_confirmed_deletion(tmp_path,deletion_state):
    db,ctx=setup(tmp_path);schema(db)
    collector=SourceCollector(db,ctx)
    collector.save('ready',{'draft_id':8,'favorite_id':7,'source_key':'123','detail':{'skus':[{}]},'observed_at':time.time()-30000})
    journal(db,account(ctx),'8','a','123',deletion_state,{})
    calls=[]
    async def call(path,method='GET',query=None,body=None):
        calls.append((path,method,query))
        if path.endswith('favorite/lists'):
            return {'data':[{'id':7,'sku':'123','is_imported':1}],'total':1}
        if path.endswith('collect/lists'):return {'data':[],'total':0}
        if path.endswith('edit_import'):return {'jump_id':9}
        if path.endswith('/detail'):
            if query['id']==8:raise Pending('old source absent')
            return {'skus':[{}]}
        raise AssertionError(path)
    collector.call=call
    if deletion_state=='deleted':
        assert (await collector.collect())['draft_id']==9
        assert sum(m=='POST' for _,m,_ in calls)==1
        assert not any(q and q.get('id')==8 for _,_,q in calls)
    else:
        with pytest.raises(Pending):await collector.collect()
        assert not any(m=='POST' for _,m,_ in calls)
