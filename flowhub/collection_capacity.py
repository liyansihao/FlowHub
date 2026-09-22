"""Account-scoped, durable reservations for new ERP source drafts only."""
import hashlib
import json
import time

from .modules import Pending


def settings(db):
    path = db.directory / 'collection-capacity.json'
    value = json.loads(path.read_text()) if path.exists() else {}
    result = {'enabled': False, 'stop_ratio': .95, 'resume_ratio': .80, 'max_age': 60} | value
    if not 0 < result['resume_ratio'] < result['stop_ratio'] <= 1 or not 1 <= result['max_age'] <= 300:
        raise ValueError('invalid_collection_capacity_policy')
    return result


def account(context):
    return hashlib.sha256((context['owner'] + ':' + context['store']['credentials']['erp_token']).encode()).hexdigest()


def schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS collection_capacity(account TEXT PRIMARY KEY,used INTEGER,capacity INTEGER,observed REAL,blocked INTEGER)')
    c.execute('CREATE TABLE IF NOT EXISTS collection_reservations(account TEXT,source_key TEXT,state TEXT,updated REAL,PRIMARY KEY(account,source_key))')


def observe(db, scope, header, read_started):
    if not settings(db)['enabled']:
        return
    if not isinstance(header, dict):
        raise Pending('collection_capacity_unavailable')
    used, limit = header.get('used', header.get('total')), header.get('limit')
    if any(isinstance(v, bool) or not str(v).isdigit() for v in (used, limit)) or int(limit) <= 0:
        raise Pending('collection_capacity_unavailable')
    used, limit = int(used), int(limit)
    cfg = settings(db)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE'); schema(c)
        old = c.execute('SELECT * FROM collection_capacity WHERE account=?', (scope,)).fetchone()
        if old and old['observed'] > read_started:
            return  # A slow stale response must not overwrite a later observation.
        blocked = (used >= limit * cfg['resume_ratio']) if old and old['blocked'] else (used >= limit * cfg['stop_ratio'])
        c.execute('INSERT OR REPLACE INTO collection_capacity VALUES(?,?,?,?,?)', (scope, used, limit, read_started, int(blocked)))
        # Only acknowledged writes predating this read are known to be in the count.
        # Unknown writes retain reservations until exact-source recovery confirms them.
        c.execute("DELETE FROM collection_reservations WHERE account=? AND state='confirmed' AND updated<=?", (scope, read_started))


async def guard(collector, *, reserve=False):
    db = collector.db; cfg = settings(db)
    if not cfg['enabled']:
        return
    scope = account(collector.c)
    from .pipeline_modules.database_work import run as database_work
    def read_observation():
        with db.connect() as c:
            schema(c)
            return c.execute('SELECT * FROM collection_capacity WHERE account=?', (scope,)).fetchone()
    row = await database_work(read_observation)
    if not row or not 0 <= time.time() - row['observed'] < cfg['max_age']:
        started = time.time()
        header = await collector.call('/api.product.collect/lists', query={'page': 1, 'page_size': 1})
        await database_work(observe, db, scope, header, started)
    def reserve_capacity():
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM collection_capacity WHERE account=?', (scope,)).fetchone()
            if not row or not 0 <= time.time() - row['observed'] < cfg['max_age']:
                raise Pending('collection_capacity_unavailable')
            pending = c.execute('SELECT count(*) FROM collection_reservations WHERE account=?', (scope,)).fetchone()[0]
            if row['blocked'] or row['used'] + pending >= row['capacity'] * cfg['stop_ratio']:
                raise Pending('collection_box_full: capacity admission paused')
            if reserve:
                if not c.execute('INSERT OR IGNORE INTO collection_reservations VALUES(?,?,?,?)',
                                 (scope, collector.key, 'unknown', time.time())).rowcount:
                    raise Pending('source draft outcome unresolved; reservation retained')
    await database_work(reserve_capacity)


def finish(collector, *, rejected=False):
    if not settings(collector.db)['enabled']:
        return
    with collector.db.connect() as c:
        schema(c)
        key = (account(collector.c), collector.key)
        if rejected:
            c.execute('DELETE FROM collection_reservations WHERE account=? AND source_key=?', key)
        else:
            c.execute("UPDATE collection_reservations SET state='confirmed',updated=? WHERE account=? AND source_key=?", (time.time(), *key))
