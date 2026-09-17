import importlib.util
import json
import sys
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from flowhub.cluster import Coordinator, create_app
from flowhub.cluster_erp import ERPRelay, validate


def setup(tmp_path):
    clock=[100.0];hub=Coordinator(tmp_path, lambda:clock[0]);relay=ERPRelay(hub)
    device=hub.enroll(hub.invitation('Windows'),'Windows')
    relay.claim(device['token'])
    return clock,hub,relay,device


def request():
    return {'url':'https://api.maozierp.com/api.product.online/batch_update_stock',
            'method':'POST','headers':{'Authorization':'Bearer secret-test'},'body':'{}'}


def test_commands_never_replay_and_payload_encrypted(tmp_path):
    clock,hub,relay,d=setup(tmp_path)
    identity=relay.submit(d['device_id'],request())
    assert b'secret-test' not in hub.path.read_bytes()
    task=relay.claim(d['token'])
    assert 'payload' not in task
    assert relay.claim(d['token'])==task
    assert relay.begin(d['token'],**dict(identity=task['id'],lease=task['lease']))==request()
    with pytest.raises(HTTPException):relay.begin(d['token'],task['id'],task['lease'])
    assert relay.claim(d['token']) is None
    clock[0]+=25
    assert relay.result(identity)=={'error':'remote_outcome_unknown'}
    assert relay.claim(d['token']) is None
    with pytest.raises(HTTPException):relay.complete(d['token'],task['id'],task['lease'],{})
    with hub.connect() as c:
        assert c.execute('SELECT payload FROM erp_commands').fetchone()[0] is None


def test_wrong_device_revocation_stale_agent_and_restart(tmp_path):
    clock,hub,relay,d=setup(tmp_path)
    other=hub.enroll(hub.invitation('other'),'Windows')
    identity=relay.submit(d['device_id'],request());task=relay.claim(d['token'])
    assert relay.claim(other['token']) is None
    with pytest.raises(HTTPException):relay.begin(other['token'],task['id'],task['lease'])
    relay=ERPRelay(Coordinator(tmp_path,lambda:clock[0]))
    relay.begin(d['token'],task['id'],task['lease'])
    result={'status':200,'body':'{"code":1}','headers':{}}
    assert relay.complete(d['token'],task['id'],task['lease'],result)['ok']
    assert relay.complete(d['token'],task['id'],task['lease'],result)['ok']
    assert relay.result(identity)==result
    with pytest.raises(HTTPException):relay.complete(d['token'],task['id'],task['lease'],{})
    clock[0]+=11
    with pytest.raises(ValueError):relay.submit(d['device_id'],request())
    hub.revoke(d['device_id'])
    with pytest.raises(HTTPException):relay.claim(d['token'])


def test_old_agent_cannot_claim_production_and_api_contract(tmp_path):
    client=TestClient(create_app(tmp_path));hub=client.app.state.hub;relay=ERPRelay(hub)
    d=hub.enroll(hub.invitation('pc'),'Windows');headers={'Authorization':'Bearer '+d['token']}
    assert client.post('/v2/erp/claim').status_code==401
    assert client.post('/v2/erp/claim',headers=headers).status_code==200
    identity=relay.submit(d['device_id'],request())
    assert hub.claim(d['token']) is None
    task=client.post('/v2/erp/claim',headers=headers).json()['task']
    assert client.post('/v2/erp/begin',headers=headers,json=task).json()==request()
    assert client.post('/v2/erp/begin',headers=headers,json=task).status_code==409
    result={'status':200,'body':'{}','headers':{}}
    assert client.post('/v2/erp/complete',headers=headers,json={**task,'result':result}).status_code==200
    assert relay.result(identity)==result


@pytest.mark.parametrize('url',[
    'http://api.maozierp.com/api.shop/lists',
    'https://api.maozierp.com.evil.test/api.shop/lists',
    'https://user@api.maozierp.com/api.shop/lists',
    'https://api.maozierp.com/api.product.online/delete',
    'https://api.maozierp.com/api.shop/../lists',
])
def test_disallowed_destinations(url):
    with pytest.raises(ValueError):validate({**request(),'url':url,'method':'GET'})


def test_windows_executor_never_retries_network_failure(tmp_path):
    directory=Path(__file__).resolve().parents[1]/'packaging/windows-agent'
    sys.path.insert(0,str(directory))
    try:
        spec=importlib.util.spec_from_file_location('production_agent',directory/'production_agent.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        class Opener:
            calls=0
            def open(self,*a,**kw):
                self.calls+=1;raise TimeoutError()
        class Client:opener=Opener()
        client=Client()
        assert module.execute(client,request())=={'error':'remote_outcome_unknown'}
        assert client.opener.calls==1
        assert module.execute(client,{**request(),'url':'https://example.com'})=={'error':'endpoint_rejected'}
        assert client.opener.calls==1
    finally:sys.path.remove(str(directory))


def test_routing_is_exact_and_offline_windows_never_falls_back(tmp_path):
    from flowhub.cluster_routing import route_bridge
    class Bridge:
        script=Path('original.mjs')
    bridge=Bridge()
    route_bridge(bridge,tmp_path,('owner','1','2'))
    directory=tmp_path/'cluster';directory.mkdir()
    (directory/'production-routing.json').write_text(json.dumps({
        'enabled':True,'device':'offline','products':[['owner','1','2']]}))
    route_bridge(bridge,tmp_path,('owner','99','2'))
    assert bridge.script==Path('original.mjs')
    with pytest.raises(BlockingIOError):route_bridge(bridge,tmp_path,('owner','1','2'))
    assert bridge.script==Path('original.mjs')
