import asyncio
import sqlite3
import threading
import time

import pytest

from flowhub.pipeline_modules import repair_facts
from flowhub.pipeline_modules.repair import PriceRepairModule
from tests.test_maozi_field_repair import setup


@pytest.mark.parametrize('cancel', [False, True])
async def test_facts_checkpoint_drains_without_blocking_other_items(tmp_path, monkeypatch, cancel):
    db, owner = setup(tmp_path)
    blocker = sqlite3.connect(db.path, check_same_thread=False)
    timers = []

    async def facts(db, owner, sku, seller, product, *args):
        blocker.execute('BEGIN IMMEDIATE')
        timer = threading.Timer(.5, blocker.commit)
        timers.append(timer)
        timer.start()
        return product

    monkeypatch.setattr(repair_facts, 'supplement', facts)
    try:
        began = time.monotonic()
        task = asyncio.create_task(PriceRepairModule().run_stage(db, owner, '1', '2', stage='facts'))
        await asyncio.sleep(.03)
        assert timers
        assert time.monotonic() - began < .25
        assert not task.done()
        if cancel:
            task.cancel()
            await asyncio.sleep(.03)
            assert not task.done()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)['reason'] == 'basic_facts_checkpointed'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM plugin_repair_events').fetchone()[0] == 1
    finally:
        for timer in timers:
            timer.join()
        blocker.close()
