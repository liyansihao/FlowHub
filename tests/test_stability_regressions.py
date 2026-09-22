"""Isolated incident reproductions. No production connections or network calls."""
import asyncio
import json
import sqlite3
import threading
import time

import pytest

from flowhub.db import Database


@pytest.mark.asyncio
async def test_capacity_guard_stays_responsive_behind_real_sqlite_writer(tmp_path):
    from flowhub import collection_capacity as capacity
    db = Database(tmp_path)
    (tmp_path / 'collection-capacity.json').write_text(json.dumps({'enabled': True}))
    with db.connect() as c:
        capacity.schema(c)
    class Collector:
        c = {'owner': 'test', 'store': {'credentials': {'erp_token': 'test'}}}
        key = 'test-sku'
        async def call(self, *args, **kwargs):
            return {'used': 1, 'limit': 100}
    collector = Collector()
    collector.db = db
    writer = sqlite3.connect(db.path, check_same_thread=False)
    writer.execute('BEGIN IMMEDIATE')
    timer = threading.Timer(.4, writer.commit)
    timer.start()
    pulse = []
    async def heartbeat():
        await asyncio.sleep(.02)
        pulse.append(time.monotonic())
    started = time.monotonic()
    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        await capacity.guard(collector)
        await task
        lag = pulse[0] - started
        print('capacity_guard_heartbeat_delay_seconds', round(lag, 3))
        assert lag < .2
    finally:
        timer.join()
        writer.close()


@pytest.mark.asyncio
async def test_listing_loop_restarts_without_sqlite_health_dependency(tmp_path, monkeypatch):
    from flowhub import listing_controls
    from flowhub.pipeline_modules import control, supervision
    db = Database(tmp_path)
    calls = []
    recovered = asyncio.Event()
    async def tick(db):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError('secret-must-not-be-logged')
        recovered.set()
        await asyncio.Event().wait()
    def health(*args):
        pytest.fail('failure logging must not contend for SQLite')
    monkeypatch.setattr(listing_controls, 'tick', tick)
    monkeypatch.setattr(control, 'paused', lambda *a: False)
    monkeypatch.setattr(db, 'health', health)
    task = asyncio.create_task(supervision.run('listing', lambda: listing_controls.run(db), tmp_path, restart_delays=(.01,)))
    try:
        await asyncio.wait_for(recovered.wait(), 2)
        rows = [json.loads(line) for line in (tmp_path / 'background-tasks.jsonl').read_text().splitlines()]
        assert [row['event'] for row in rows] == ['started', 'failed', 'restarting', 'started']
        assert rows[1]['error_type'] == 'ValueError'
        assert 'secret-must-not-be-logged' not in json.dumps(rows)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_fatal_child_and_cancel_cleanup_lock_cannot_leave_partial_runtime(tmp_path, monkeypatch):
    from flowhub import plugin_pipeline as pipeline
    from flowhub import acquisition, cluster_compute, comparebot_process, listing_controls, source_gateway
    from flowhub.pipeline_modules import runtime, control, repair_workflow, repair_cleanup, isolation
    from flowhub.pipeline_modules import admission, source_loop, draft_cleanup, favorite_cleanup, store_capacity, request_bridge
    class DB:
        directory = tmp_path
        def health(self, *args):
            pass
    db = DB()
    entered = asyncio.Event()
    recovered = asyncio.Event()
    starts = []
    claims = []
    async def idle(*args, **kwargs):
        await asyncio.Event().wait()
    async def close(*args, **kwargs):
        pass
    def claim(*args, **kwargs):
        claims.append(1)
        return ({'state': 'publishing', 'body': '{}', 'attempts': 0}, ('o', '1', '2'), 'token')
    async def publish(*args):
        entered.set()
        await asyncio.Event().wait()
    def locked_release(*args):
        raise sqlite3.OperationalError('database is locked')
    async def failed_listing(*args):
        await entered.wait()
        starts.append(1)
        if len(starts) == 1:raise sqlite3.OperationalError('database is locked')
        recovered.set()
        await idle()
    original_tick = pipeline.tick
    async def selected_tick(*args, **kwargs):
        if kwargs.get('lane') == 'submit':
            return await original_tick(*args, **kwargs)
        await idle()
    monkeypatch.setattr(pipeline, 'claim', claim)
    monkeypatch.setattr(pipeline, 'advance', publish)
    monkeypatch.setattr(pipeline, 'release_lease', locked_release)
    monkeypatch.setattr(pipeline, 'tick', selected_tick)
    monkeypatch.setattr(control, 'schema', lambda *a: None)
    monkeypatch.setattr(control, 'paused', lambda *a: False)
    monkeypatch.setattr(repair_workflow, 'config', lambda *a: {'enabled': False})
    monkeypatch.setattr(acquisition, 'routing_active', lambda *a: False)
    monkeypatch.setattr(cluster_compute, 'extra_review_workers', lambda *a: 0)
    monkeypatch.setattr(repair_cleanup, 'cleanup', lambda *a: None)
    monkeypatch.setattr(isolation, 'tick', idle)
    for module in (admission, source_loop, draft_cleanup, favorite_cleanup, store_capacity):
        monkeypatch.setattr(module, 'run', idle)
    monkeypatch.setattr(listing_controls, 'run', failed_listing)
    for module, name in ((acquisition, 'close_runners'), (comparebot_process, 'close_workers'),
                         (request_bridge, 'close_requests'), (source_gateway, 'close')):
        monkeypatch.setattr(module, name, close)
    task = asyncio.create_task(runtime.run(db, submit_workers=1))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(recovered.wait(), 3)
        assert not task.done()
        assert len(claims) == 1  # Other lanes were never cancelled/restarted.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert len(claims) == 1  # Cleanup lock error cannot turn cancellation into another claim.
    finally:
        monkeypatch.setattr(pipeline, 'release_lease', lambda *a: None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_32_competing_writers_are_serialized_with_rollback(tmp_path):
    from flowhub.pipeline_modules.database_work import run
    db = Database(tmp_path)
    with db.connect() as c:c.execute('CREATE TABLE stress(n PRIMARY KEY)')
    active = 0
    peak = 0
    def write(n):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            with db.connect() as c:
                c.execute('PRAGMA busy_timeout=0')
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT INTO stress VALUES(?)', (n,))
                time.sleep(.001)  # Deliberate contention in the reproduction only.
                if n % 17 == 0:raise ValueError('rollback')
        finally:active -= 1
    async def writer(offset):
        for n in range(offset, 320, 32):
            try:await run(write, n)
            except ValueError:assert n % 17 == 0
    started = time.monotonic()
    await asyncio.gather(*(writer(i) for i in range(32)))
    with db.connect() as c:rows = {r[0] for r in c.execute('SELECT n FROM stress')}
    assert rows == {n for n in range(320) if n % 17}
    assert peak == 1
    print('stress_320_transactions_seconds', time.monotonic()-started)


@pytest.mark.asyncio
async def test_repeated_cancel_and_failed_transaction_preserve_shutdown():
    from flowhub.pipeline_modules.database_work import run
    entered = threading.Event()
    release = threading.Event()
    order = []
    def write():
        entered.set()
        assert release.wait(3)
        order.append('finished')
        raise sqlite3.OperationalError('simulated I/O error')
    task = asyncio.create_task(run(write))
    await asyncio.to_thread(entered.wait, 1)
    task.cancel();await asyncio.sleep(.01);task.cancel();await asyncio.sleep(.01)
    assert not task.done()
    following = asyncio.create_task(run(lambda: order.append('following')))
    await asyncio.sleep(.01);assert not following.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    await following
    assert order == ['finished', 'following']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['error','return','cancel'])
async def test_supervisor_bounded_failure_escalation(tmp_path, mode):
    from flowhub.pipeline_modules.supervision import run
    calls = []
    async def broken():
        calls.append(1)
        if mode == 'error':raise ValueError('broken')
        if mode == 'cancel':raise asyncio.CancelledError()
    with pytest.raises((ValueError, RuntimeError, asyncio.CancelledError)):
        await run('broken', broken, tmp_path, restart_delays=(.001,.001), healthy_after=60)
    assert len(calls) == 3
    assert json.loads((tmp_path/'background-tasks.jsonl').read_text().splitlines()[-1])['event'] == 'exhausted'


@pytest.mark.asyncio
async def test_long_read_does_not_occupy_writer_queue():
    from flowhub.pipeline_modules.database_work import run, read
    entered = threading.Event()
    release = threading.Event()
    def snapshot():
        entered.set()
        assert release.wait(3)
    task = asyncio.create_task(read(snapshot))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert await asyncio.wait_for(run(lambda: 'committed'), .3) == 'committed'
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_network_slots_remain_concurrent_after_claims(tmp_path):
    from flowhub.pipeline_modules.database_work import run
    db = Database(tmp_path)
    with db.connect() as c:c.execute('CREATE TABLE effects(id PRIMARY KEY)')
    remote_entered = set()
    all_remote = asyncio.Event()
    def persist(n):
        with db.connect() as c:c.execute('INSERT INTO effects VALUES(?)', (n,))
    async def slot(n):
        await run(lambda: None)  # Independent claim completes before remote I/O.
        remote_entered.add(n)
        if len(remote_entered) == 16:all_remote.set()
        await asyncio.wait_for(all_remote.wait(), 2)
        await run(persist, n)
    await asyncio.gather(*(slot(n) for n in range(16)))
    with db.connect() as c:assert c.execute('SELECT count(*) FROM effects').fetchone()[0] == 16
