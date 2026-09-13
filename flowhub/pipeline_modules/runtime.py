"""Bounded independent lanes; pending stock/readback has reserved capacity."""
import asyncio
from . import control


async def run(db, *, review_workers=2, submit_workers=2, reconcile_workers=2):
    from ..plugin_pipeline import tick
    from ..comparebot_process import close_workers
    if not all(1 <= n <= 4 for n in (review_workers, submit_workers, reconcile_workers)):
        raise ValueError('each lane requires 1..4 workers')
    control.schema(db)

    async def lane(name):
        module = 'seed' if name == 'seed_repair' else 'review' if name == 'review' else 'publication'
        while True:
            if control.paused(db, module):
                await asyncio.sleep(.5)
                continue
            try:
                worked = await tick(db, lane=name)
            except Exception:
                db.health('pipeline-' + name + '-error')
                worked = False
            await asyncio.sleep(.05 if worked else .5)

    tasks = [asyncio.create_task(lane(name)) for name, count in
             [('seed_repair',1), ('review', review_workers), ('submit', submit_workers), ('reconcile', reconcile_workers)] for _ in range(count)]
    from .admission import run as admission_loop
    tasks.append(asyncio.create_task(admission_loop(db)))
    from .source_loop import run as source_loop
    tasks.append(asyncio.create_task(source_loop(db)))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_workers()
