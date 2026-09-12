import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from deploy.prepare_migration import prepare
from flowhub.db import Database


def test_migration_preserves_identity_and_keys_without_source_changes(tmp_path):
    source = Database(tmp_path / 'source')
    with source.connect() as c:
        owner = c.execute('SELECT id FROM users').fetchone()[0]
        original_password = c.execute('SELECT password FROM users').fetchone()[0]
        c.execute("INSERT INTO sessions VALUES('token',?,'csrf',9999999999)", (owner,))
        c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',
                  ('store1', owner, 'shared', 'demo', '{}', source.seal({'api_key': 'test-only-key'})))
    destination = tmp_path / 'snapshot'
    manifest = prepare(source.directory, destination)
    assert manifest['counts']['stores'] == 1
    with sqlite3.connect(destination / 'flowhub.sqlite3') as c:
        assert c.execute('SELECT password FROM users').fetchone()[0] == original_password
        assert c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
        secret = c.execute('SELECT secret FROM stores').fetchone()[0]
        assert json.loads(Fernet((destination / 'master.key').read_bytes()).decrypt(secret.encode()))['api_key'] == 'test-only-key'
    with source.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
    assert destination.stat().st_mode & 0o777 == 0o700
    assert (destination / 'master.key').stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='拒绝覆盖'):
        prepare(source.directory, destination)


def test_running_workflow_cannot_be_cloned_for_execution(tmp_path):
    source = Database(tmp_path / 'source')
    with source.connect() as c:
        c.execute('UPDATE workflows SET enabled=1')
    with pytest.raises(ValueError, match='暂停'):
        prepare(source.directory, tmp_path / 'snapshot')
    assert not (tmp_path / 'snapshot').exists()
    assert not list(tmp_path.glob('.flowhub-migration-*'))
