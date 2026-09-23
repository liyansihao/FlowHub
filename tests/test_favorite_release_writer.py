import asyncio
from contextlib import contextmanager
import json
import os
import sqlite3
import threading
import time
import pytest
from flowhub.db import Database
from flowhub.pipeline_modules.favorite_release import execute_one, schema
from flowhub.pipeline_modules.database_work import run


def setup(tmp_path):
    db=Database(tmp_path/'data')
    p=dict(owner='o',sku='1',seller='s',phase='stock_verified',verified=True,offer_id='offer',store_id='store')
    source=dict(state='ready',source_key='1',favorite_id='7',draft_id='8',detail={'skus':[{}]})
    with db.connect() as c:
        schema(c)
        c.execute('CREATE TABLE plugin_publications(owner,sku,seller,body,updated)')
        c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?,?)',('o','1','s',json.dumps(p),0))
        c.execute('CREATE TABLE plugin_pipeline(owner,sku,seller,state,body)')
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?)',('o','1','s','selling',json.dumps({'offer_id':'offer'})))
        c.execute('CREATE TABLE plugin_pipeline_leases(owner,sku,seller,token,expires)')
        c.execute('CREATE TABLE source_details(key,state,body,updated)')
        c.execute('INSERT INTO source_details VALUES(?,?,?,?)',('source','ready',db.seal(source),1))
        c.execute('CREATE TABLE draft_cleanup_receipts(draft_id,state)')
        c.execute('CREATE TABLE writer_test(n)')
    @contextmanager
    def fast_connection():
        c=sqlite3.connect(db.path,timeout=.03);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    db.connect=fast_connection
    return db


class Fake:
    present=True;writes=0
    def __init__(self,on_online=None):self.on_online=on_online
    async def call(self,path,method='GET',params=None,body=None):
        if path.endswith('favorite/lists'):return {'total':int(self.present),'data':[{'id':7,'sku':'1'}] if self.present else []}
        if path.endswith('collect/detail'):return {'skus':[{}]}
        if path.endswith('online/lists'):
            if self.on_online:await self.on_online()
            return {'data':[{'shop_id':'shop','offer_id':'offer','online_status':'selling','stock':99}]}
        assert path.endswith('favorite/toggle');self.writes+=1;self.present=False;return {}


def arguments(db,api,tmp_path):
    return (db,{'account':'a','sku':'1','favorite_id':'7','source_key':'source','business_revision':'test'},api,{'store':{'client':api,'shop_id':'shop'}},tmp_path/'archive')


@pytest.mark.asyncio
async def test_shared_writer_queue_handles_contention_without_extending_timeout(tmp_path):
    db=setup(tmp_path);entered=threading.Event();tasks=[];pulses=[]
    def writer():
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE');entered.set();time.sleep(.1);c.execute('INSERT INTO writer_test VALUES(1)')
    async def online():
        tasks.extend(asyncio.create_task(run(writer)) for _ in range(30))
        await asyncio.to_thread(entered.wait,1)
    async def pulse():
        for _ in range(40):pulses.append(time.monotonic());await asyncio.sleep(.01)
    api=Fake(online);heartbeat=asyncio.create_task(pulse())
    result=await execute_one(*arguments(db,api,tmp_path));await asyncio.gather(*tasks,heartbeat)
    assert result['state']=='deleted' and api.writes==1
    assert max(b-a for a,b in zip(pulses,pulses[1:]))<.1
    with db.connect() as c:assert c.execute('SELECT count(*) FROM writer_test').fetchone()[0]==30


@pytest.mark.asyncio
async def test_sidecar_cannot_bypass_production_writer_queue(tmp_path):
    db=setup(tmp_path);(db.directory/'runtime-worker.json').write_text(json.dumps({'identity':{'pid':os.getpid()+1}}));api=Fake()
    with pytest.raises(RuntimeError,match='production worker writer queue'):await execute_one(*arguments(db,api,tmp_path))
    assert api.writes==0


@pytest.mark.asyncio
async def test_batch_records_sqlite_location_and_stops_instead_of_expanding(tmp_path,monkeypatch):
    from flowhub.pipeline_modules import favorite_release_batch as batch
    db=setup(tmp_path)
    (db.directory/'runtime-worker.json').write_text(json.dumps({'identity':{'pid':os.getpid(),'source_revision':'v'}}))
    request={'enabled':True,'batch_id':'b','revision':'v','max_deletes':20,'seconds':30,'items':[{'sku':'1','favorite_id':'7','source_key':'source'},{'sku':'2','favorite_id':'8','source_key':'other'}]}
    (db.directory/'favorite-release-request.json').write_text(json.dumps(request));api=Fake();calls=[]
    monkeypatch.setattr(batch,'context',lambda db,item,proxy=None:(item,api,{}))
    async def fail(*args):calls.append(1);raise sqlite3.OperationalError('test busy')
    monkeypatch.setattr(batch,'execute_one',fail)
    await batch.tick(db);await batch.tick(db)
    state=json.loads((db.directory/'favorite-release-batch-b.json').read_text())
    assert len(calls)==1 and state['state']=='stopped_database_error'
    assert state['records'][0]['result']['error']['frames'][-1]['function']=='fail'

@pytest.mark.asyncio
async def test_batch_bound_and_completed_request_never_repeat(tmp_path,monkeypatch):
    from flowhub.pipeline_modules import favorite_release_batch as batch
    db=setup(tmp_path)
    (db.directory/'runtime-worker.json').write_text(json.dumps({'identity':{'pid':os.getpid(),'source_revision':'v'}}))
    request={'enabled':True,'batch_id':'bounded','revision':'v','max_deletes':2,'seconds':30,'items':[{'sku':str(i),'favorite_id':str(i),'source_key':str(i)} for i in range(4)]}
    (db.directory/'favorite-release-request.json').write_text(json.dumps(request));api=Fake();calls=[]
    monkeypatch.setattr(batch,'context',lambda db,item,proxy=None:(item,api,{}))
    async def deleted(db,item,*args):calls.append(item['sku']);return {'state':'deleted'}
    monkeypatch.setattr(batch,'execute_one',deleted)
    assert await batch.tick(db)
    assert not await batch.tick(db)
    assert calls==['0','1']
    state=json.loads((db.directory/'favorite-release-batch-bounded.json').read_text())
    assert state['state']=='finished' and len(state['records'])==2


def test_batch_proxy_is_explicit_loopback_only():
    from flowhub.pipeline_modules.favorite_release_batch import validate_proxy
    assert validate_proxy(None) is None
    assert validate_proxy('http://127.0.0.1:7897')=='http://127.0.0.1:7897'
    for value in ('http://external.example:7897','http://user:secret@127.0.0.1:7897','http://127.0.0.1:7897/x','http://localhost:7897'):
        with pytest.raises(ValueError):validate_proxy(value)
