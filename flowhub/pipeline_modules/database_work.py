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
