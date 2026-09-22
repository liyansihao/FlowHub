import importlib.util
import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from flowhub.cluster import create_app
from flowhub.runtime_identity import capture, report_loop, save


def report(tmp_path, component='erp-agent'):
    (tmp_path / 'production_agent.py').write_text('pass\n')
    return capture(component, tmp_path, {'erp': 2})


def test_startup_snapshot_is_immutable_and_private_files_do_not_affect_identity(tmp_path):
    first = report(tmp_path)
    (tmp_path / 'device.json').write_text('{"token":"TOP_SECRET"}')
    (tmp_path / '.env').write_text('PASSWORD=TOP_SECRET')
    second = capture('erp-agent', tmp_path, {'erp': 2})
    assert first['source_sha256'] == second['source_sha256']
    (tmp_path / 'production_agent.py').write_text('changed = True\n')
    third = capture('erp-agent', tmp_path, {'erp': 2})
    assert third['source_sha256'] != first['source_sha256']
    save(tmp_path, first)
    saved = (tmp_path / 'runtime-erp-agent.json').read_text()
    assert json.loads(saved)['identity']['source_sha256'] == first['source_sha256']
    assert 'TOP_SECRET' not in saved and str(tmp_path) not in saved


def test_authenticated_reports_are_device_bound_and_do_not_change_readiness(tmp_path):
    app = create_app(tmp_path / 'cluster')
    client = TestClient(app)
    hub = app.state.hub
    a = hub.enroll(hub.invitation('a'), 'Windows')
    b = hub.enroll(hub.invitation('b'), 'Windows')
    body = report(tmp_path)
    headers = {'Authorization': 'Bearer ' + a['token']}
    assert client.post('/v1/runtime', json=body).status_code == 401
    assert client.post('/v1/runtime', json=body, headers=headers).status_code == 200
    assert client.post('/v1/runtime', json=body, headers=headers).status_code == 200
    assert client.post('/v1/runtime', json={**body, 'source_sha256': '0'*64}, headers=headers).status_code == 409
    assert client.post('/v1/runtime', json={**body, 'device': b['device_id']}, headers=headers).status_code == 422
    assert client.post('/v1/runtime', json={**body, 'token': 'private'}, headers=headers).status_code == 422
    with hub.connect() as c:
        assert c.execute('SELECT device FROM runtime_reports').fetchone()[0] == a['device_id']
        assert c.execute('SELECT COUNT(*) FROM erp_devices').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM compute_devices').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    hub.revoke(a['device_id'])
    assert client.post('/v1/runtime', json=body, headers=headers).status_code == 401
    assert client.get('/v1/runtime').status_code == 405


def test_old_clients_still_claim_and_health_keeps_startup_snapshot(tmp_path):
    app = create_app(tmp_path / 'cluster')
    client = TestClient(app)
    hub = app.state.hub
    device = hub.enroll(hub.invitation('old'), 'Windows')
    headers = {'Authorization': 'Bearer ' + device['token']}
    assert client.post('/v2/erp/claim', headers=headers).json() == {'task': None}
    assert client.post('/v3/compute/register', headers=headers, json={
        'version': 3, 'slots': 1, 'kinds': ['rank'], 'cpu_count': 4, 'accelerator': 'cpu'}).status_code == 200
    a = client.get('/healthz').json()
    b = client.get('/healthz').json()
    assert a['erp_protocol'] == 2 and a['compute_protocol'] == 3
    assert a['runtime'] == b['runtime']


def test_report_failure_does_not_claim_or_execute_anything(tmp_path):
    stop = threading.Event()
    calls = []
    class OldClient:
        def call(self, path, body):
            calls.append(path)
            stop.set()
            raise OSError('old coordinator unavailable')
    report_loop(OldClient(), report(tmp_path), stop)
    assert calls == ['/v1/runtime']


def test_inventory_unknown_stale_and_duplicate_processes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('version_inventory', root / 'scripts/runtime_inventory.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = create_app(tmp_path / 'cluster')
    hub = app.state.hub
    device = hub.enroll(hub.invitation('pc'), 'Windows')
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + device['token']}
    assert module.inventory(tmp_path)['devices'][0]['version_status'] == 'missing_stale_or_multiple'
    for component in ('erp-agent', 'full-agent', 'compute-worker'):
        assert client.post('/v1/runtime', json=report(tmp_path, component), headers=headers).status_code == 200
    assert module.inventory(tmp_path)['devices'][0]['version_status'] == 'reported'
    assert module.inventory(tmp_path, now=hub.clock()+100)['devices'][0]['version_status'] == 'missing_stale_or_multiple'
    assert client.post('/v1/runtime', json=report(tmp_path), headers=headers).status_code == 200
    assert module.inventory(tmp_path)['devices'][0]['fresh_component_counts']['erp-agent'] == 2
    assert module.inventory(tmp_path)['devices'][0]['version_status'] == 'missing_stale_or_multiple'


def test_packaged_helper_matches_server_source():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'flowhub/runtime_identity.py').read_bytes() == (root / 'packaging/windows-agent/runtime_identity.py').read_bytes()
