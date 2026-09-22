import concurrent.futures
import json
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from flowhub.cluster import Coordinator,create_app
from flowhub.cluster_compute import Compute,extra_review_workers


CAP={'version':3,'slots':1,'kinds':['rank','screen','dossier'],'cpu_count':8,'accelerator':'cpu'}


def setup(tmp_path):
    clock=[100.];hub=Coordinator(tmp_path/'cluster',lambda:clock[0]);service=Compute(hub)
    d=hub.enroll(hub.invitation('Windows'),'Windows');service.register(d['token'],CAP)
    policy=hub.directory/'compute-policy.json'
    policy.write_text(json.dumps({'enabled':True,'devices':[d['device_id']],'kinds':['rank','screen','dossier']}))
    return clock,hub,service,d


def test_busy_capacity_parallel_claim_and_expiration(tmp_path):
    clock,hub,service,d=setup(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        jobs=list(pool.map(lambda i:service.submit('rank',{'manifest':{'product_id':str(i)}}),range(2)))
    assert sum(x is not None for x in jobs)==1
    task=service.claim(d['token']);assert task
    assert service.claim(d['token']) is None
    assert service.submit('screen',{}) is None
    restarted=Compute(hub)
    result={'output':{'query':{'product_id':'1'}}}
    assert restarted.complete(d['token'],task['id'],task['lease'],task['digest'],result)['ok']
    assert restarted.complete(d['token'],task['id'],task['lease'],task['digest'],result)['ok']
    assert service.result(task['id'])==result
    with hub.connect() as c:
        assert c.execute('SELECT samples FROM compute_metrics WHERE device=?',(d['device_id'],)).fetchone()[0]==1
    identity=service.submit('rank',{'api_key':'private-key'})
    assert b'private-key' not in hub.path.read_bytes()
    stale=service.claim(d['token']);clock[0]+=181
    with pytest.raises(HTTPException):service.complete(d['token'],stale['id'],stale['lease'],stale['digest'],result)
    assert service.result(identity)=={'error':'compute_deadline'}
    assert service.claim(d['token']) is None


def test_device_input_and_result_binding(tmp_path):
    clock,hub,service,d=setup(tmp_path)
    other=hub.enroll(hub.invitation('other'),'Windows')
    identity=service.submit('screen',{'api_key':'secret'})
    assert service.claim(other['token']) is None
    task=service.claim(d['token'])
    for token,digest in [(other['token'],task['digest']),(d['token'],'wrong')]:
        with pytest.raises(HTTPException):service.complete(token,task['id'],task['lease'],digest,{})
    hub.revoke(d['device_id'])
    with pytest.raises(HTTPException):service.complete(d['token'],task['id'],task['lease'],task['digest'],{})


def test_api_old_devices_and_readiness_gates(tmp_path):
    app=create_app(tmp_path/'cluster');client=TestClient(app);hub=app.state.hub
    service=Compute(hub);d=hub.enroll(hub.invitation('Windows'),'Windows')
    (hub.directory/'compute-policy.json').write_text(json.dumps({'enabled':True,'devices':[d['device_id']]}))
    assert service.submit('rank',{}) is None
    headers={'Authorization':'Bearer '+d['token']}
    assert client.post('/v3/compute/register',json=CAP).status_code==401
    assert client.post('/v3/compute/register',json=CAP,headers=headers).status_code==200
    assert extra_review_workers(tmp_path)==1
    assert service.submit('rank',{})
    assert hub.claim(d['token']) is None
    assert client.post('/v3/compute/register',json={**CAP,'kinds':['exec']},headers=headers).status_code==422
    client.post('/v3/compute/register',json={**CAP,'kinds':[]},headers=headers)
    assert extra_review_workers(tmp_path)==0


@pytest.mark.asyncio
async def test_remote_review_keeps_product_and_supplier_bindings(monkeypatch):
    from flowhub import comparebot
    from flowhub import cluster_compute
    from flowhub.modules import ModuleError
    candidate={'source_key':'123','title':'product','image':'https://example.com/a.jpg'}
    async def wrong_product(*a,**kw):return {'query':{'product_id':'999'},'candidates':[]}
    monkeypatch.setattr(cluster_compute,'remote',wrong_product)
    with pytest.raises(ModuleError,match='product binding'):await comparebot.screen(candidate)
    async def wrong_supplier(*a,**kw):return {'search_and_rank':{'query':{'product_id':'123'},'candidates':[{'changed':True}]}}
    monkeypatch.setattr(cluster_compute,'remote',wrong_supplier)
    with pytest.raises(ModuleError,match='supplier binding'):await comparebot.screen(candidate,ranking={'candidates':[]})
    async def no_candidates(*a,**kw):return {'query':{'product_id':'123'},'candidates':[],'no_candidates':True}
    monkeypatch.setattr(cluster_compute,'remote',no_candidates)
    assert (await comparebot.screen(candidate))['decision']['outcome']=='manual_review'


def test_dossier_extraction_cannot_switch_source_or_variant():
    from flowhub.dossier_packet import extract
    payload={'sku':'123','snapshot':{'source_key':'123','detail':{'skus':[{}],'package_weight':55,'common_attributes':[{'id':1}]}}}
    result=extract(payload)
    assert result['fields']['weight_g']==55
    assert result['fields']['dimensions_mm']==[None,None,None]
    with pytest.raises(ValueError):extract({**payload,'sku':'other'})
    with pytest.raises(ValueError):extract({'sku':'123','snapshot':{'source_key':'123','detail':{'skus':[{},{}]}}})


def test_status_reader_does_not_block_compute_result(tmp_path):
    import sqlite3
    clock,hub,service,d=setup(tmp_path)
    identity=service.submit('rank',{'manifest':{'product_id':'123'}})
    task=service.claim(d['token'])
    reader=sqlite3.connect(hub.path)
    try:
        assert reader.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
        reader.execute('BEGIN')
        assert reader.execute('SELECT state FROM compute_jobs WHERE id=?',(identity,)).fetchone()[0]=='running'
        # A status page can hold its snapshot while a device returns a result.
        # Bound the writer timeout so the old rollback journal fails promptly.
        original_connect=hub.connect
        from contextlib import contextmanager
        @contextmanager
        def short_connection():
            with original_connect() as c:
                c.execute('PRAGMA busy_timeout=100')
                yield c
        hub.connect=short_connection
        result={'output':{'query':{'product_id':'123'}}}
        assert service.complete(d['token'],identity,task['lease'],task['digest'],result)['ok']
        assert reader.execute('SELECT state FROM compute_jobs WHERE id=?',(identity,)).fetchone()[0]=='running'
        reader.rollback()
        assert reader.execute('SELECT state FROM compute_jobs WHERE id=?',(identity,)).fetchone()[0]=='done'
        assert service.result(identity)==result
    finally:
        reader.close()
