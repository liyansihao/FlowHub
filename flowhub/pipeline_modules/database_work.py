"""Serialize complete DB units off the event loop; never detach cancelled writes."""
import asyncio
import logging
import weakref
from functools import partial

_gates = weakref.WeakKeyDictionary()


async def drain(task):
    """Wait for real completion even when shutdown cancels the waiter again."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def run(function, *args, **kwargs):
    loop = asyncio.get_running_loop()
    gate = _gates.setdefault(loop, asyncio.Lock())
    # Queue in asyncio, before consuming a thread or opening a transaction.
    # Only synchronous DB units enter here; network work remains concurrent.
    async with gate:
        return await read(function, *args, **kwargs)


async def read(function, *args, **kwargs):
    """Run an audited read-only unit without occupying the writer queue."""
    task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await drain(task)
        except Exception as error:
            logging.getLogger(__name__).error('cancelled_db_unit_failed function=%s type=%s',
                getattr(function, '__name__', type(function).__name__), type(error).__name__)
        raise


async def cancelled_claim(task, cleanup):
    """Drain a shielded claim and release only the exact token it acquired."""
    async def finish():
        result = await drain(task)
        if result:
            await cleanup(result)
    await cancelled_cleanup(finish())


async def cancelled_cleanup(coroutine):
    # Shield the whole cleanup, including time spent queued, from repeated cancel.
    task = asyncio.create_task(coroutine)
    try:
        await drain(task)
    except Exception as error:
        logging.getLogger(__name__).error('cancel_cleanup_failed type=%s', type(error).__name__)
        # Keep the original cancellation; an unsuccessful cleanup retains its
        # token-fenced lease until expiry. Never dispatch new remote work here.


async def health(db,name):
    """A contended diagnostic write must not shut down the publishing lanes."""
    import sqlite3
    try:
        await run(db.health,name)
    except sqlite3.OperationalError as error:
        if not any(word in str(error).lower() for word in ('locked','busy')):raise
        return False
    return True
