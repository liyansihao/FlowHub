import json
import sqlite3
import pytest
from flowhub.db import Database
from flowhub.sqlite_diagnostics import TimedConnection


def test_diagnostics_disabled_by_default(tmp_path,monkeypatch):
    monkeypatch.delenv('FLOWHUB_SQLITE_DIAGNOSTICS',raising=False)
    with Database(tmp_path).connect() as c:
        assert type(c) is sqlite3.Connection


def test_preserves_commit_rollback_and_never_logs_sql_values(tmp_path,monkeypatch):
    monkeypatch.setenv('FLOWHUB_SQLITE_DIAGNOSTICS','1')
    db=Database(tmp_path)
    with db.connect() as c:
        assert isinstance(c,TimedConnection)
        c.execute('CREATE TABLE probe(value TEXT)')
        c.execute('INSERT INTO probe VALUES(?)',('secret-value-do-not-log',))
        c.opened-=3
    with pytest.raises(ValueError):
        with db.connect() as c:
            c.execute('INSERT INTO probe VALUES(?)',('rollback-secret',))
            c.opened-=3
            raise ValueError('caller-error')
    with db.connect() as c:
        assert [x[0] for x in c.execute('SELECT * FROM probe')]==['secret-value-do-not-log']
    text=(tmp_path/'sqlite-transactions.jsonl').read_text()
    assert 'secret-value' not in text and 'rollback-secret' not in text and 'INSERT INTO' not in text
    records=[json.loads(line) for line in text.splitlines()]
    assert all(r['transactions'] for r in records)


def test_explicit_commit_splits_transaction_timings(tmp_path):
    c=sqlite3.connect(tmp_path/'probe.sqlite',factory=TimedConnection)
    c.execute('CREATE TABLE probe(value TEXT)')
    c.execute('BEGIN IMMEDIATE');c.commit()
    c.execute('BEGIN IMMEDIATE');c.rollback()
    assert len(c.transactions)==2
    assert c.writer_started is None
    c.close()


def test_lock_wait_not_recorded_as_acquired_transaction(tmp_path):
    path=tmp_path/'probe.sqlite'
    writer=sqlite3.connect(path);writer.execute('CREATE TABLE probe(value TEXT)');writer.execute('BEGIN IMMEDIATE')
    c=sqlite3.connect(path,factory=TimedConnection,timeout=.02)
    try:
        with pytest.raises(sqlite3.OperationalError,match='locked'):c.execute('BEGIN IMMEDIATE')
        assert c.writer_started is None and not c.transactions
        assert c.slow_operations[-1]['error_type']=='OperationalError'
    finally:c.close();writer.rollback();writer.close()


def test_diagnostic_io_failure_preserves_transaction(tmp_path,monkeypatch):
    monkeypatch.setenv('FLOWHUB_SQLITE_DIAGNOSTICS','1');db=Database(tmp_path)
    (tmp_path/'sqlite-transactions.jsonl').mkdir()
    with db.connect() as c:
        c.execute('CREATE TABLE probe(value INTEGER)');c.execute('INSERT INTO probe VALUES(1)');c.opened-=3
    with db.connect() as c:assert c.execute('SELECT count(*) FROM probe').fetchone()[0]==1
