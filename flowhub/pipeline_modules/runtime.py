"""Bounded independent lanes; pending stock/readback has reserved capacity."""
import asyncio
from . import control, supervision


async def publication_tick(db, name, index, tick):
    """Lend idle slots, keeping one readback slot and all existing SKU leases."""
    worked = await tick(db, lane=name)
    if not worked and name=='submit':
        return await tick(db, lane='reconcile')
    if not worked and name=='reconcile' and index>0:
        return await tick(db, lane='submit')
    return worked


async def run(db, *, review_workers=2, submit_workers=2, reconcile_workers=2, seed_workers=1):
    from .database_work import run as database_work, drain
    from ..plugin_pipeline import tick
    from ..comparebot_process import close_workers
    if not all(1 <= n <= 4 for n in (review_workers, submit_workers, reconcile_workers)):
        raise ValueError('each lane requires 1..4 workers')
    if seed_workers not in (1,2):raise ValueError('seed workers must be 1 or a guarded trial of 2')
    await database_work(control.schema,db)
    from . import repair_workflow
    repair_policy=repair_workflow.config(db)
    from ..acquisition import routing_active
    operation_repairs=routing_active(db)

    async def lane(name, index=0):
        module = 'seed' if name in ('seed_repair','repair_valuation','repair_publication','repair_validation','repair_acquisition') else 'review' if name in ('review','remote_review') else 'publication'
        while True:
            if name=='remote_review':
                from ..cluster_compute import extra_review_workers
                if index>=await database_work(extra_review_workers,db.directory):
                    await asyncio.sleep(5)
                    continue
            if await database_work(control.paused,db, module):
                await asyncio.sleep(.5)
                continue
            if name=='seed_repair' and index>0:
                from .repair_queue import can_expand
                if not await database_work(can_expand,db):
                    await asyncio.sleep(5)
                    continue
            if name=='repair_validation':
                worked=await tick(db,lane='seed_repair',repair_kind='publication',repair_stage='validate')
            elif name in ('repair_publication','repair_acquisition') and operation_repairs:
                worked=await tick(db,lane='seed_repair',repair_kind='publication',repair_stage='acquire',acquisition_route=name=='repair_acquisition')
            elif name in ('repair_valuation','repair_publication'):
                worked=await tick(db,lane='seed_repair',repair_kind=name.removeprefix('repair_'))
            elif name=='seed_repair' and seed_workers==2:
                from .repair_queue import tick_repair
                worked=await tick_repair(db,index,tick)
            elif name in ('submit','reconcile'):
                worked = await publication_tick(db,name,index,tick)
            else:
                worked = await tick(db, lane='review' if name=='remote_review' else name)
            await asyncio.sleep(60 if name=='reconcile_history' and worked else 5 if name=='reconcile_history' else .05 if worked else .5)

    async def repair_maintenance():
        from .repair_cleanup import cleanup
        while True:
            await database_work(cleanup,db)
            await asyncio.sleep(60)

    async def isolated_readback():
        from .isolation import tick as inspect_isolated
        while True:
            await inspect_isolated(db)
            await asyncio.sleep(15)

    repairs=[('repair_valuation',repair_policy['valuation_workers']),('repair_publication',repair_policy['publication_workers'])] if repair_policy['enabled'] else [('seed_repair',seed_workers)]
    if operation_repairs and repair_policy['enabled']:repairs.extend([('repair_validation',1),('repair_acquisition',1)])
    tasks = []
    def start(name, factory):
        tasks.append(asyncio.create_task(supervision.run(name, factory, db.directory), name=name))
    for name, count in repairs+[('review', review_workers), ('submit', submit_workers), ('reconcile', reconcile_workers), ('reconcile_history',1), ('remote_review',2)]:
        for index in range(count):
            start(f'{name}:{index}', lambda name=name, index=index: lane(name,index))
    start('repair_maintenance', repair_maintenance)
    start('isolated_readback', isolated_readback)
    from ..listing_controls import run as listing_control_loop
    from .admission import run as admission_loop
    from .source_loop import run as source_loop
    from .draft_cleanup import run as draft_cleanup
    from .favorite_cleanup import run as favorite_cleanup
    from .store_capacity import run as capacity_loop
    from .favorite_release_batch import run as favorite_release
    for name, factory in [('listing_controls',listing_control_loop),('admission',admission_loop),
                          ('source',source_loop),('draft_cleanup',draft_cleanup),
                          ('favorite_cleanup',favorite_cleanup),('favorite_release',favorite_release),('store_capacity',capacity_loop)]:
        start(name, lambda factory=factory: factory(db))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await drain(asyncio.gather(*tasks, return_exceptions=True))
        from ..acquisition import close_runners
        await close_runners()
        await close_workers()
        from .request_bridge import close_requests
        await close_requests()
        from ..source_gateway import close as close_acquisition
        await close_acquisition()
