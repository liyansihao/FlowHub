import json
import time

import pytest

from flowhub.collection_capacity import account, schema as capacity_schema
from flowhub.collection_recheck import recheck
from flowhub.db import Database
from flowhub.source_detail import SourceCollector


def setup(tmp_path):
    db=Database(tmp_path)
    ctx={'owner':'a','candidate':{'source_key':'123'},
         'store':{'config':{},'credentials':{'erp_token':'token'}}}
    collector=SourceCollector(db,ctx)
    collector.save('draft_started',{'source_key':'123','favorite_id':7})
    with db.connect() as c:
        capacity_schema(c)
        c.execute('INSERT INTO collection_reservations VALUES(?,?,?,?)',
                  (account(ctx),collector.key,'unknown',time.time()))
        c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',
                  ('store','a','store','maozi','{}',db.seal({'erp_token':'token'})))
    return db,collector


@pytest.mark.asyncio
async def test_exact_original_draft_confirms_unknown_reservation_without_write(tmp_path,monkeypatch):
    db,collector=setup(tmp_path);calls=[]
    async def read(self,path,method='GET',query=None,body=None):
        calls.append((path,method,query))
        if path.endswith('/lists'):
            return {'total':1,'data':[{'id':8,'goods_id':'123','collect_from':'ozon'}]}
        return {'skus':[{'id':1}]}
    monkeypatch.setattr(SourceCollector,'call',read)
    assert (await recheck(db))['confirmed']==1
    with db.connect() as c:
        assert c.execute('SELECT state FROM collection_reservations').fetchone()[0]=='confirmed'
        row=c.execute('SELECT state,body FROM source_details WHERE key=?',(collector.key,)).fetchone()
        assert row['state']=='ready' and db.open(row['body'])['draft_id']==8
    assert all(method=='GET' for _,method,_ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('listing',[
    {'total':0,'data':[]},
    {'total':2,'data':[{'id':8,'goods_id':'123','collect_from':'ozon'}]},
])
async def test_absent_or_incomplete_draft_retains_unknown_reservation(tmp_path,monkeypatch,listing):
    db,collector=setup(tmp_path)
    async def read(self,path,method='GET',query=None,body=None):return listing
    monkeypatch.setattr(SourceCollector,'call',read)
    result=await recheck(db)
    assert result['confirmed']==0
    with db.connect() as c:
        assert c.execute('SELECT state FROM collection_reservations').fetchone()[0]=='unknown'
        assert c.execute('SELECT state FROM source_details WHERE key=?',(collector.key,)).fetchone()[0]=='draft_started'


@pytest.mark.asyncio
async def test_missing_original_account_credentials_never_queries_or_releases(tmp_path,monkeypatch):
    db,collector=setup(tmp_path)
    with db.connect() as c:c.execute('UPDATE stores SET enabled=0')
    async def forbidden(*args,**kwargs):raise AssertionError('wrong account must not read remotely')
    monkeypatch.setattr(SourceCollector,'call',forbidden)
    result=await recheck(db)
    assert result['unresolved']==1
    with db.connect() as c:
        assert c.execute('SELECT state FROM collection_reservations').fetchone()[0]=='unknown'
