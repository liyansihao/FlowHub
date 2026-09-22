"""Run complete synchronous DB units off the event loop; drain on cancellation."""
import asyncio
from functools import partial


async def run(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # SQLite work cannot be cancelled by cancelling its awaiter. Never let a
        # transaction continue after its caller has released a publication lock.
        await task
        raise


async def health(db,name):
    """A contended diagnostic write must not shut down the publishing lanes."""
    import sqlite3
    try:
        await run(db.health,name)
    except sqlite3.OperationalError as error:
        if not any(word in str(error).lower() for word in ('locked','busy')):raise
        return False
    return True
