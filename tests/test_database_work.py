import asyncio
import sqlite3
import threading
import time

import httpx
import pytest

from flowhub import plugin_pipeline as pipeline
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.pipeline_modules.database_work import run


def configured(tmp_path):
    db=Database(tmp_path);SourceLibrary(db);pipeline.schema(db)
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'1','2','publishing','{}',0,0))
    return db


@pytest.mark.asyncio
async def test_writer_lock_does_not_block_event_loop_or_duplicate_claim(tmp_path,monkeypatch):
    db=configured(tmp_path);calls=[]
    async def publish(*args):
        calls.append(args);await asyncio.sleep(.03)
        return {'phase':'stock_verified','verified':True}
    monkeypatch.setattr(pipeline,'advance',publish)
    blocker=sqlite3.connect(db.path,check_same_thread=False);blocker.execute('BEGIN IMMEDIATE')
    timer=threading.Timer(.4,blocker.commit);timer.start()
    try:
        started=time.monotonic();tasks=[asyncio.create_task(pipeline.tick(db,lane='submit')) for _ in range(2)]
        await asyncio.sleep(.03)
        assert time.monotonic()-started<.2
        assert sorted(await asyncio.gather(*tasks))==[False,True]
        assert len(calls)==1
    finally:timer.join();blocker.close()


@pytest.mark.asyncio
async def test_cancelled_claim_drains_then_releases_its_lease(tmp_path,monkeypatch):
    db=configured(tmp_path)
    async def publish(*args):pytest.fail('cancelled claim must never publish')
    monkeypatch.setattr(pipeline,'advance',publish)
    blocker=sqlite3.connect(db.path,check_same_thread=False);blocker.execute('BEGIN IMMEDIATE')
    timer=threading.Timer(.2,blocker.commit);timer.start()
    try:
        task=asyncio.create_task(pipeline.tick(db,lane='submit'));await asyncio.sleep(.02);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        with db.connect() as c:assert c.execute('SELECT count(*) FROM plugin_pipeline_leases').fetchone()[0]==0
    finally:timer.join();blocker.close()


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
async def test_official_reservation_lock_does_not_stop_other_coroutines(tmp_path):
    from flowhub.official_api import OfficialTransport
    db=Database(tmp_path);requests=[]
    def response(r):requests.append(r);return httpx.Response(200,json={'result':[]})
    transport=OfficialTransport(db,{'client_id':'1','api_key':'test'},transport=httpx.MockTransport(response))
    blocker=sqlite3.connect(db.path,check_same_thread=False);blocker.execute('BEGIN IMMEDIATE')
    timer=threading.Timer(.3,blocker.commit);timer.start()
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            started=time.monotonic();task=asyncio.create_task(client.post('https://api-seller.ozon.ru/v2/warehouse/list',json={}))
            await asyncio.sleep(.02);assert time.monotonic()-started<.15
            assert not requests
            assert (await task).status_code==200
    finally:timer.join();blocker.close()


@pytest.mark.asyncio
async def test_generic_worker_claim_lock_keeps_loop_responsive(tmp_path):
    from flowhub.worker import Worker
    db=Database(tmp_path);worker=Worker(db)
    blocker=sqlite3.connect(db.path,check_same_thread=False);blocker.execute('BEGIN IMMEDIATE')
    timer=threading.Timer(.3,blocker.commit);timer.start()
    try:
        began=time.monotonic();task=asyncio.create_task(worker.step())
        await asyncio.sleep(.02);assert time.monotonic()-began<.15
        assert await task is False
    finally:timer.join();blocker.close()


@pytest.mark.asyncio
async def test_busy_claim_defers_without_crashing_worker(tmp_path,monkeypatch):
    from flowhub.worker import Worker
    worker=Worker(Database(tmp_path))
    def busy():raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(worker,'claim',busy)
    assert await worker.step() is False
    def broken():raise sqlite3.OperationalError('no such table: jobs')
    monkeypatch.setattr(worker,'claim',broken)
    with pytest.raises(sqlite3.OperationalError,match='no such table'):await worker.step()


@pytest.mark.asyncio
async def test_remote_snapshot_does_not_stall_loop_and_drains_before_sync_unlock(tmp_path,monkeypatch):
    import fcntl
    from flowhub import remote_reviews
    from test_manual_reviews import setup
    db,owner=setup(tmp_path);entered=threading.Event();release=threading.Event()
    monkeypatch.setenv('FLOWHUB_REVIEW_SYNC_URL','https://review.invalid/api/reviews')
    monkeypatch.setenv('FLOWHUB_REVIEW_SYNC_TOKEN','test-token')
    monkeypatch.setenv('FLOWHUB_REVIEW_OWNER',owner)
    def slow_snapshot(*args):
        entered.set();assert release.wait(3);return {'items':[],'total':0}
    monkeypatch.setattr(remote_reviews,'current_snapshot',slow_snapshot)
    original=httpx.AsyncClient
    monkeypatch.setattr(remote_reviews.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'items':[]})),**kw))
    task=asyncio.create_task(remote_reviews.once(db))
    assert await asyncio.to_thread(entered.wait,2)
    task.cancel();await asyncio.sleep(.02);assert not task.done()
    with (db.directory/'remote-review-sync.lock').open('a') as lock:
        with pytest.raises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
