import asyncio
import sqlite3
import threading
import pytest
from flowhub.db import Database
from flowhub.pipeline_modules.database_work import run


@pytest.mark.asyncio
async def test_cancelled_db_unit_finishes_before_caller_releases_resources():
    entered=threading.Event();release=threading.Event();finished=[]
    def transaction():entered.set();release.wait(2);finished.append(True)
    task=asyncio.create_task(run(transaction))
    await asyncio.to_thread(entered.wait,1);task.cancel();await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert finished==[True]


@pytest.mark.asyncio
async def test_contended_error_health_does_not_stop_lanes(tmp_path,monkeypatch):
    from flowhub.pipeline_modules.database_work import health
    db=Database(tmp_path)
    def busy(*args):raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(db,'health',busy)
    assert await health(db,'test-error') is False
    def broken(*args):raise sqlite3.OperationalError('no such table: health')
    monkeypatch.setattr(db,'health',broken)
    with pytest.raises(sqlite3.OperationalError,match='no such table'):await health(db,'test-error')
