"""Pause new admissions to stores with observed exhausted capacity; probe for recovery."""
import asyncio,json,time
from .database_work import run as database_work, health as database_health


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS store_publication_capacity(
        owner TEXT,store_id TEXT,state TEXT,body TEXT,next_check REAL,
        PRIMARY KEY(owner,store_id))''')


def observe(db,owner,store_id,quota,now=None):
    from ..plugin_publication import require_quota
    now=time.time() if now is None else now
    try:require_quota(quota);state='ready'
    except ValueError:state='blocked'
    with db.write_transaction() as c:
        schema(c)
        c.execute('INSERT OR REPLACE INTO store_publication_capacity VALUES(?,?,?,?,?)',
                  (owner,store_id,state,json.dumps({'at':now,'quota':quota}),now+900))
    return state


def claim_check(db):
    from .control import paused
    if paused(db,'publication'):return None
    now=time.time()
    with db.connect() as c:
        schema(c)
        row=c.execute('''SELECT q.*,s.config,s.secret FROM store_publication_capacity q
            JOIN stores s ON s.owner=q.owner AND s.id=q.store_id
            WHERE q.state='blocked' AND q.next_check<=? AND s.verified=1
            ORDER BY q.next_check LIMIT 1''',(now,)).fetchone()
        if row:
            c.execute('UPDATE store_publication_capacity SET next_check=? WHERE owner=? AND store_id=?',(now+900,row['owner'],row['store_id']))
        return row


async def check_one(db):
    from ..maozi import MaoziPublisher
    row=await database_work(claim_check,db)
    if not row:return
    cfg=json.loads(row['config'])
    keys=db.open(row['secret'])
    if keys.get('client_id') and keys.get('api_key') and cfg.get('official_observations',True):
        from ..official_api import client, capacity
        async with client(db,keys) as api:quota=await capacity(api)
    else:
        api=MaoziPublisher({'store':{'config':cfg,'credentials':keys}})
        quota=await api.erp('POST','/api.shop/sync_single_product_limit',body={'id':int(cfg['shop_id'])})
    await database_work(observe,db,row['owner'],row['store_id'],quota)


async def run(db):
    while True:
        try:await check_one(db)
        except Exception:await database_health(db,'store-capacity-check-error')
        await asyncio.sleep(60)
