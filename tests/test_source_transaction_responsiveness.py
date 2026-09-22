import asyncio
import sqlite3
import threading
import time

import pytest

from flowhub.db import Database
from flowhub.source_acquisition import SourceAcquirer
from flowhub.source_library import SourceLibrary


@pytest.mark.parametrize('cancel', [False, True])
async def test_shop_checkpoint_wait_keeps_loop_alive_and_drains(tmp_path, cancel):
    library = SourceLibrary(Database(tmp_path))
    now = time.time()
    library.enqueue('owner', 'own_shop', {'shop_id': '7'}, 100)
    task = library.claim('owner', now, kinds=('own_shop',))

    async def request(*args):
        return {'data': [], 'last_page': 1}

    acquirer = SourceAcquirer(library, request=request)
    acquirer.delist_snapshot = {'skus': [], 'offers': []}
    blocker = sqlite3.connect(library.db.path, check_same_thread=False)
    blocker.execute('BEGIN IMMEDIATE')
    timer = threading.Timer(.5, blocker.commit)
    timer.start()
    try:
        started = time.monotonic()
        operation = asyncio.create_task(acquirer.own_shop(task, 'test', now))
        await asyncio.sleep(.03)
        assert time.monotonic() - started < .25
        assert not operation.done()
        if cancel:
            operation.cancel()
            await asyncio.sleep(.03)
            assert not operation.done()  # The database unit still owns its lease.
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            assert (await operation)['finished']
        with library.db.connect() as c:
            row = c.execute('SELECT lease,successes FROM sourcing_tasks WHERE id=?', (task['id'],)).fetchone()
            assert row['lease'] is None
            assert row['successes'] == 1
    finally:
        timer.join()
        blocker.close()


async def test_failed_source_checkpoint_does_not_block_unrelated_coroutines(tmp_path):
    library = SourceLibrary(Database(tmp_path))
    library.enqueue('owner', 'own_shop', {'shop_id': '7'}, 100)
    blocker = sqlite3.connect(library.db.path, check_same_thread=False)
    timers = []

    async def request(*args):
        blocker.execute('BEGIN IMMEDIATE')
        timer = threading.Timer(.5, blocker.commit)
        timer.start()
        timers.append(timer)
        raise RuntimeError('test source failure')

    async def delists():
        return {'skus': [], 'offers': []}

    acquirer = SourceAcquirer(library, request=request, delists=delists)
    try:
        started = time.monotonic()
        operation = asyncio.create_task(acquirer.cycle('owner', 'test'))
        # Wait until the external failure has begun its durable checkpoint.
        while not timers:
            await asyncio.sleep(.001)
        await asyncio.sleep(.03)
        assert time.monotonic() - started < .25
        assert not operation.done()
        result = await operation
        assert result['state'] == 'retry'
        with library.db.connect() as c:
            row = c.execute('SELECT failures,lease FROM sourcing_tasks').fetchone()
            assert row['failures'] == 1
            assert row['lease'] is None
    finally:
        for timer in timers:
            timer.join()
        blocker.close()
