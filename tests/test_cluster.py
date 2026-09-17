import concurrent.futures
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from flowhub.cluster import Coordinator,create_app


def tokens(hub,n=2):return [hub.enroll(hub.invitation(str(i)),'Windows')['token'] for i in range(n)]

def test_enrollment_one_time_expiring_and_revocable(tmp_path):
    clock=[100];hub=Coordinator(tmp_path,lambda:clock[0]);code=hub.invitation('PC')
    device=hub.enroll(code,'Windows')
    with pytest.raises(HTTPException):hub.enroll(code,'Windows')
    hub.revoke(device['device_id'])
    with pytest.raises(HTTPException):hub.claim(device['token'])
    code=hub.invitation('other');clock[0]+=1801
    with pytest.raises(HTTPException):hub.enroll(code,'Windows')


def test_parallel_claim_dedup_and_lost_response(tmp_path):
    hub=Coordinator(tmp_path);a,b=tokens(hub);task=hub.enqueue('sku-same')
    assert hub.enqueue('sku-same')==task
    with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(hub.claim,(a,b)))
    assert sum(r is not None for r in results)==1
    winner=a if results[0] else b
    assert hub.claim(winner)==next(r for r in results if r)


def test_disconnect_reclaim_rejects_old_writer_and_result_is_idempotent(tmp_path):
    clock=[100];hub=Coordinator(tmp_path,lambda:clock[0]);a,b=tokens(hub);hub.enqueue('one')
    old=hub.claim(a);clock[0]+=121;new=hub.claim(b)
    assert old['id']==new['id'] and old['lease']!=new['lease']
    with pytest.raises(HTTPException):hub.heartbeat(a,old['id'],old['lease'])
    result={'challenge':old['payload']['challenge'],'platform':'Windows'}
    with pytest.raises(HTTPException):hub.complete(a,old['id'],old['lease'],result)
    assert hub.complete(b,new['id'],new['lease'],result)=={'ok':True,'duplicate':False}
    assert hub.complete(b,new['id'],new['lease'],result)['duplicate']
    with pytest.raises(HTTPException):hub.complete(b,new['id'],new['lease'],dict(result,platform='changed'))
    assert Coordinator(tmp_path).status()['tasks']=={'done':1}


def test_api_requires_token_and_validates_result(tmp_path):
    app=create_app(tmp_path);hub=app.state.hub;client=TestClient(app)
    assert client.post('/v1/claim').status_code==401
    d=client.post('/v1/enroll',json={'code':hub.invitation('Windows'),'platform':'Windows'}).json()
    headers={'Authorization':'Bearer '+d['token']};hub.enqueue('api')
    task=client.post('/v1/claim',headers=headers).json()['task']
    payload={'task_id':task['id'],'lease':task['lease'],'challenge':'bad','platform':'Windows'}
    assert client.post('/v1/complete',headers=headers,json=payload).status_code==422
    payload['challenge']=task['payload']['challenge']
    assert client.post('/v1/complete',headers=headers,json=payload).status_code==200
    assert client.get('/healthz').json()['mode']=='acceptance_only'
    assert client.post('/v1/publish',headers=headers,json={}).status_code==404
