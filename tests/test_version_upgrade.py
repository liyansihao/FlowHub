import importlib.util
import json
from contextlib import ExitStack
from pathlib import Path

import pytest


def load():
    p = Path(__file__).resolve().parents[1] / 'packaging/windows-agent/upgrade_versions.py'
    spec = importlib.util.spec_from_file_location('version_upgrade', p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup(tmp_path):
    m = load()
    root, bundle = tmp_path / 'agent', tmp_path / 'bundle'
    root.mkdir(); bundle.mkdir()
    protected = {'device.json': b'{"token":"private"}', 'device.commands.sqlite3': b'ledger',
                 'device.erp-result.json': b'pending result'}
    for name, value in protected.items():
        (root / name).write_bytes(value)
    manifest = {'files': {}, 'check_only': {}}
    for name in m.FILES:
        if name != 'runtime_identity.py':
            (root / name).write_text('old-' + name)
        (bundle / name).write_text('new-' + name)
        manifest['files'][name] = {'old': m.sha(root / name), 'new': m.sha(bundle / name)}
    for name in m.CHECK_ONLY:
        (root / name).write_text('dependency')
        manifest['check_only'][name] = m.sha(root / name)
    (bundle / 'version-update.json').write_text(json.dumps(manifest))
    return m, root, bundle, protected


def test_upgrade_preserves_credentials_ledgers_results_and_is_idempotent(tmp_path):
    m, root, bundle, protected = setup(tmp_path)
    assert m.upgrade(root, bundle)['ok']
    assert m.upgrade(root, bundle)['already_installed']
    for name, body in protected.items():
        assert (root / name).read_bytes() == body
    for name in m.FILES:
        assert m.sha(root / name) == m.sha(bundle / name)


def test_busy_worker_refuses_upgrade(tmp_path):
    m, root, bundle, _ = setup(tmp_path)
    before = m.sha(root / 'production_agent.py')
    with ExitStack() as stack:
        m.acquire(root, stack)
        with pytest.raises(OSError):
            m.upgrade(root, bundle)
    assert m.sha(root / 'production_agent.py') == before


@pytest.mark.parametrize('case', ['installed_change', 'bundle_change', 'path_escape'])
def test_unexpected_code_or_bundle_rejected_before_any_copy(tmp_path, case):
    m, root, bundle, _ = setup(tmp_path)
    before = m.sha(root / 'production_agent.py')
    if case == 'installed_change':
        (root / 'compute_worker.py').write_text('custom code')
    elif case == 'bundle_change':
        (bundle / 'compute_worker.py').write_text('bad package')
    else:
        p = bundle / 'version-update.json'
        d = json.loads(p.read_text());d['files']['../device.json'] = {}
        p.write_text(json.dumps(d))
    with pytest.raises(ValueError):
        m.upgrade(root, bundle)
    assert m.sha(root / 'production_agent.py') == before


def test_partial_copy_error_restores_original_code(tmp_path, monkeypatch):
    m, root, bundle, _ = setup(tmp_path)
    before = {name: m.sha(root / name) for name in m.FILES}
    replace = m.os.replace
    count = [0]
    def fail_second(*args):
        count[0] += 1
        if count[0] == 2:
            raise OSError('simulated disk failure')
        return replace(*args)
    monkeypatch.setattr(m.os, 'replace', fail_second)
    with pytest.raises(OSError):
        m.upgrade(root, bundle)
    assert {name: m.sha(root / name) for name in m.FILES} == before
