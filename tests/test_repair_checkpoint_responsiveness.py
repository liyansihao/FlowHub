import asyncio
import sqlite3
import threading
import time

import pytest

from flowhub.maozi import MaoziPublisher
from flowhub.pipeline_modules.repair import PriceRepairModule
from tests.test_maozi_field_repair import setup


@pytest.mark.parametrize('cancel', [False, True])
async def test_facts_checkpoint_drains_without_blocking_other_items(tmp_path, monkeypatch, cancel):
    db, owner = setup(tmp_path)
    blocker = sqlite3.connect(db.path, check_same_thread=False)
    timers = []

    async def facts(self, *args, **kwargs):
        blocker.execute('BEGIN IMMEDIATE')
        timer = threading.Timer(.5, blocker.commit)
        timers.append(timer)
        timer.start()
        return {'status':{'update_sales':False},'data':{'sku':'1','sellerId':'2','avgPrice':300}}

    monkeypatch.setattr(MaoziPublisher, 'erp', facts)
    try:
        began = time.monotonic()
        task = asyncio.create_task(PriceRepairModule().run(db, owner, '1', '2'))
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
            assert (await task)['state'] == 'ready'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM plugin_repair_events').fetchone()[0] == 1
    finally:
        for timer in timers:
            timer.join()
        blocker.close()


async def test_repair_event_and_product_rollback_together(tmp_path, monkeypatch):
    from flowhub.source_library import SourceLibrary
    db, owner = setup(tmp_path)
    with db.connect() as c:
        before = c.execute('SELECT body FROM sourcing_products').fetchone()[0]
    async def erp(self, *args, **kwargs):
        return {'status':{'update_sales':False},'data':{'sku':'1','sellerId':'2','avgPrice':300}}
    original = SourceLibrary.put
    def fail_after_write(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError('injected checkpoint failure')
    monkeypatch.setattr(MaoziPublisher, 'erp', erp)
    monkeypatch.setattr(SourceLibrary, 'put', fail_after_write)
    with pytest.raises(RuntimeError, match='injected checkpoint failure'):
        await PriceRepairModule().run(db, owner, '1', '2')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM plugin_repair_events').fetchone()[0] == 0
        assert c.execute('SELECT body FROM sourcing_products').fetchone()[0] == before
