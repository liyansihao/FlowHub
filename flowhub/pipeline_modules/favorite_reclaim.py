"""Account inventory reclamation; retain live consumers, archive unused favorites."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .database_work import run as database_work

_held = ContextVar('favorite_guards', default=())


@contextmanager
def source_guard(directory, sku):
    # Task identity prevents a child task inheriting a context from bypassing flock.
    identity = (str(directory), str(sku), id(asyncio.current_task()))
    if identity in _held.get():
        yield
        return
    directory = Path(directory) / 'favorite-dependency-locks'
    directory.mkdir(exist_ok=True)
    with (directory / (hashlib.sha256(str(sku).encode()).hexdigest()+'.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        token = _held.set((*_held.get(), identity))
        try:
            yield
        finally:
            _held.reset(token)


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS favorite_reclaim_batches(
        account TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS favorite_capacity_state(
        account TEXT PRIMARY KEY, used INTEGER NOT NULL, capacity INTEGER NOT NULL,
        observed REAL NOT NULL, blocked_until REAL NOT NULL)''')


def capacity_values(header):
    used, limit = header.get('used'), header.get('limit')
    if any(isinstance(x, bool) or not str(x).isdigit() for x in (used, limit)) or int(limit) <= 0:
        raise ValueError('invalid_favorite_capacity')
    return int(used), int(limit)


def observe(db, account, header):
    used, limit = capacity_values(header)
    with db.connect() as c:
        schema(c)
        c.execute('INSERT OR REPLACE INTO favorite_capacity_state VALUES(?,?,?,?,?)',
                  (account, used, limit, time.time(), time.time()+60 if used >= limit else 0))


def dependency_proof(db, context, favorite):
    """All local owners of this SKU; absence is user-authorized, read failure is not."""
    sku = str(favorite['sku']); now = time.time()
    proof = {'sku': sku, 'favorite': favorite, 'observed_at': now,
             'publications': [], 'queues': [], 'jobs': [], 'sources': [], 'products': []}
    reasons = []
    token_hash = hashlib.sha256(context['store']['credentials']['erp_token'].encode()).hexdigest()
    with db.connect() as c:
        exists = lambda table: c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
        owners = [r[0] for r in c.execute('SELECT id FROM users')]
        for owner in owners:
            # Use the existing owner+SKU indexes; never scan the multi-GB publication bodies.
            if exists('plugin_pipeline_leases') and c.execute(
                'SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND expires>?',
                (owner, sku, now)).fetchone():
                reasons.append('active_lease')
            if exists('plugin_pipeline'):
                for row in c.execute('SELECT * FROM plugin_pipeline WHERE owner=? AND sku=?', (owner, sku)):
                    proof['queues'].append(dict(row))
                    if row['state'] not in ('selling', 'rejected', 'not_listed', 'delisted'):
                        reasons.append('pipeline:'+row['state'])
            if exists('plugin_publications'):
                for row in c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=?', (owner, sku)):
                    p = json.loads(row[0]); proof['publications'].append(p)
                    if not (p.get('phase') == 'stock_verified' and p.get('verified') is True):
                        reasons.append('publication:'+str(p.get('phase')))
            if exists('jobs'):
                for row in c.execute('SELECT * FROM jobs WHERE owner=? AND source_key=?', (owner, sku)):
                    job = dict(row); proof['jobs'].append(job)
                    if row['lease_until'] > now or row['phase'] not in ('selling', 'rejected', 'cancelled', 'delisted'):
                        reasons.append('job:'+row['phase'])
                    elif row['phase'] == 'selling':
                        p = json.loads(row['data']).get('external_publication', {})
                        if not (p.get('phase') == 'stock_verified' and p.get('verified') is True
                                and p.get('offer_id') == row['id']):
                            reasons.append('legacy_job')
            if exists('sourcing_products'):
                proof['products'].extend(dict(r) for r in c.execute(
                    'SELECT * FROM sourcing_products WHERE owner=? AND sku=?', (owner, sku)))
            key = hashlib.sha256((owner+':'+token_hash+':'+sku).encode()).hexdigest()
            if exists('source_details'):
                row = c.execute('SELECT * FROM source_details WHERE key=?', (key,)).fetchone()
                if row:
                    source = dict(row); source['body'] = db.open(source['body']); proof['sources'].append(source)
                    if row['state'] not in ('ready', 'draft_ready', 'favorite_rejected', 'draft_rejected'):
                        reasons.append('source:'+row['state'])
            if exists('acquisition_bindings'):
                for row in c.execute('''SELECT t.body,t.lease_until FROM acquisition_tasks t
                    JOIN acquisition_bindings b ON b.task_key=t.key WHERE b.owner=? AND b.sku=?''', (owner, sku)):
                    work = db.open(row['body'])
                    if row['lease_until'] > now or work.get('stage') != 'ready' or work.get('unknown_favorite') or work.get('unknown_draft'):
                        reasons.append('acquisition')
            scope = hashlib.sha256((owner+':'+context['store']['credentials']['erp_token']).encode()).hexdigest()
            if exists('collection_reservations') and c.execute(
                "SELECT 1 FROM collection_reservations WHERE account=? AND source_key=? AND state='unknown'",
                (scope, key)).fetchone():
                reasons.append('unknown_source_write')
    journal = Path(db.directory) / 'plugin-production.sqlite3'
    if journal.exists():
        with sqlite3.connect(f'file:{journal}?mode=ro', uri=True, timeout=3) as c:
            rows = c.execute('SELECT offer_id,phase,details FROM zero_stock_tests WHERE sku=?', (sku,)).fetchall()
        proof['journal'] = [dict(offer_id=r[0], phase=r[1], details=json.loads(r[2])) for r in rows]
        if any(r[1] != 'stock_verified' for r in rows):
            reasons.append('publication_journal')
    return sorted(set(reasons)), proof


def save_archive(db, account, context, favorite, settings):
    from . import favorite_cleanup as old
    from . import control
    if control.paused(db, 'seed') or not old.config(db)['enabled']:
        return 'paused', None
    reasons, proof = dependency_proof(db, context, favorite)
    if reasons:
        return 'protected:'+','.join(reasons), None
    proof['policy'] = 'reclaim_unused_including_unrecorded'
    proof['authorization'] = settings['authorization']
    proof['account'] = account
    directory = Path(db.directory) / 'favorite-lineage-archive'
    directory.mkdir(mode=0o700, exist_ok=True)
    encoded = json.dumps(proof, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(encoded).hexdigest(); path = directory / (digest+'.json')
    with path.open('xb') as f:
        os.chmod(path, 0o600); f.write(encoded); f.flush(); os.fsync(f.fileno())
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('archive_verification_failed')
    proof.update(archive_path=str(path), archive_sha256=digest)
    # Preserve imported identity even for user-authorized unrecorded favorites.
    # This is a positive deduplication fact, never permission to publish.
    journal = Path(db.directory) / 'plugin-production.sqlite3'
    if str(favorite.get('is_imported')) in ('1', 'True') and journal.exists():
        with sqlite3.connect(journal, timeout=3) as c:
            c.execute('CREATE TABLE IF NOT EXISTS confirmed_source_imports(sku TEXT PRIMARY KEY,source TEXT NOT NULL)')
            c.execute('INSERT OR IGNORE INTO confirmed_source_imports VALUES(?,?)',
                      (str(favorite['sku']), 'favorite_reclaim_archive:'+digest))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        changed = c.execute('INSERT OR IGNORE INTO favorite_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
            (account, str(favorite['id']), context['owner'], str(favorite['sku']), 'intent', db.seal(proof), time.time())).rowcount
    return ('intent', proof) if changed else ('receipt_retained', None)


async def clean_one(db, account, context, favorite, settings, client):
    from . import favorite_cleanup as old
    sku, fid = str(favorite['sku']), str(favorite['id'])
    with source_guard(db.directory, sku):
        prior = await database_work(old.receipt, db, account, fid)
        if prior:
            if prior['state'] == 'deleted':
                return 'receipt_retained'
            return await old.reconcile_one(db, account, prior, client, lambda: False)
        # Explicit views avoid the ERP default hiding imported records.
        current = []
        for imported in (0, 1):
            header = await client.call('/api.product.favorite/lists', params={'sku': sku, 'is_imported': imported, 'page': 1, 'page_size': 100})
            rows = header.get('data')
            if not isinstance(rows, list) or str(header.get('total')) != str(len(rows)) or any(str(r.get('sku')) != sku for r in rows):
                raise ValueError('incomplete_exact_favorite_lookup')
            current.extend(rows)
        exact = [r for r in current if str(r['id']) == fid]
        if len(exact) != 1 or len({str(r['id']) for r in current}) != 1:
            return 'favorite_absent' if not exact else 'favorite_ambiguous'
        state, proof = await database_work(save_archive, db, account, context, exact[0], settings)
        if state != 'intent':
            return state
        try:
            proof['response'] = await client.call('/api.product.favorite/toggle', method='POST', body={
                'status': False, 'productInfo': exact[0] | {'sku': sku, 'coverImage': exact[0].get('cover_image', ''),
                'price_info': {'sell_price': exact[0].get('sell_price'), 'currency': 'CNY'}}})
        except Exception as error:
            proof['error_type'] = type(error).__name__
            await database_work(old.update_receipt, db, account, fid, 'unconfirmed', proof)
        else:
            await database_work(old.update_receipt, db, account, fid, 'acknowledged', proof)
        prior = await database_work(old.receipt, db, account, fid)
        return await old.reconcile_one(db, account, prior, client, lambda: False)


def contexts(db):
    with db.connect() as c:
        rows = c.execute('''SELECT s.* FROM stores s JOIN pipeline_campaigns p ON p.owner=s.owner
            JOIN users u ON u.id=s.owner WHERE p.enabled=1 AND u.active=1 AND s.enabled=1''').fetchall()
    found = {}
    for row in rows:
        keys = db.open(row['secret'])
        if keys.get('erp_token'):
            context = {'owner': row['owner'], 'store': {'config': json.loads(row['config']), 'credentials': keys}}
            account = hashlib.sha256((row['owner']+':'+keys['erp_token']).encode()).hexdigest()
            found[account] = context
    return found


def load_batch(db, account):
    with db.connect() as c:
        schema(c)
        row = c.execute('SELECT body FROM favorite_reclaim_batches WHERE account=?', (account,)).fetchone()
    return json.loads(row[0]) if row else None


def save_batch(db, account, batch):
    with db.connect() as c:
        schema(c)
        c.execute('INSERT OR REPLACE INTO favorite_reclaim_batches VALUES(?,?,?)', (account, json.dumps(batch), time.time()))


async def inventory(client):
    found = {}; header = None
    for imported in (0, 1):
        expected = None; seen = set()
        for page in range(1, 101):
            header = await client.call('/api.product.favorite/lists', params={'is_imported': imported, 'page': page, 'page_size': 100})
            rows = header.get('data'); total = header.get('total')
            if not isinstance(rows, list) or not str(total).isdigit():
                raise ValueError('invalid_favorite_inventory')
            if expected is not None and expected != int(total):
                raise ValueError('favorite_inventory_changed')
            expected = int(total)
            for row in rows:
                fid, sku = str(row.get('id')), str(row.get('sku'))
                if not fid.isdigit() or not sku.isdigit() or fid in seen:
                    raise ValueError('favorite_inventory_identity')
                seen.add(fid); found[fid] = {'id': fid, 'sku': sku}
            if len(seen) == expected:
                break
            if not rows:
                raise ValueError('incomplete_favorite_inventory')
        else:
            raise ValueError('favorite_inventory_limit')
    return header, list(found.values())


async def tick(db, settings, client_factory=None):
    from . import favorite_cleanup as old
    from . import control
    client_factory = client_factory or old.Client
    result = []
    await database_work(old.schema, db)
    for account, context in (await database_work(contexts, db)).items():
        client = client_factory(context)
        header = await client.call('/api.product.favorite/lists', params={'page': 1, 'page_size': 1})
        await database_work(observe, db, account, header)
        used, limit = capacity_values(header)
        batch = await database_work(load_batch, db, account)
        if not batch or batch['state'] == 'complete':
            if used < limit * settings['threshold_ratio']:
                result.append({'state': 'below_threshold', 'used': used, 'limit': limit})
                continue
            _, rows = await inventory(client)
            batch = {'state': 'active', 'started': time.time(), 'pending': rows, 'retained': [], 'deleted': 0, 'scanned': 0}
            await database_work(save_batch, db, account, batch)
        batch['state']='active'
        started = time.monotonic(); checked = 0; errors = 0
        while batch['pending'] and checked < settings['max_checks_per_cycle'] and time.monotonic()-started < settings['cycle_seconds']:
            if await database_work(control.paused, db, 'seed') or not old.config(db)['enabled']:
                break
            if settings.get('verification_delete_limit') and batch['deleted']>=settings['verification_delete_limit']:
                batch['state']='canary_hold'
                break
            item = batch['pending'][0]
            try:
                reason = await clean_one(db, account, context, item, settings, client)
            except BlockingIOError:
                reason = 'busy'
            except Exception as error:
                batch['last_error']={'type':type(error).__name__,'at':time.time(),
                                     'reason':str(error)[:160] if isinstance(error,ValueError) else type(error).__name__}
                await database_work(save_batch,db,account,batch)
                errors += 1
                break  # Retain cursor/receipt; bounded next-cycle retry.
            if reason in ('busy', 'unconfirmed_present', 'paused'):
                batch['retained'].append(item | {'reason': reason})
            elif reason == 'deleted':
                batch['deleted'] += 1
            else:
                batch['retained'].append(item | {'reason': reason})
            batch['pending'].pop(0); batch['scanned'] += 1; checked += 1
            await database_work(save_batch, db, account, batch)
        if not batch['pending']:
            # Revisit protected records once after progress, so consumers which
            # completed during the batch are reclaimed too. Stop after a no-progress sweep.
            if batch['retained'] and batch.get('last_sweep_deleted', 0) != batch['deleted']:
                batch['pending'] = [{k: r[k] for k in ('id', 'sku')} for r in batch['retained']]
                batch['retained'] = []; batch['last_sweep_deleted'] = batch['deleted']
            else:
                retry = [r for r in batch['retained'] if r['reason'] in ('busy', 'unconfirmed_present', 'paused')]
                if retry:
                    batch['pending'] = [{k:r[k] for k in ('id','sku')} for r in retry]
                    batch['retained'] = [r for r in batch['retained'] if r not in retry]
                    batch['state'] = 'waiting'
                else:
                    batch['state'] = 'complete'; batch['finished'] = time.time()
            await database_work(save_batch, db, account, batch)
        await database_work(save_batch, db, account, batch)
        header = await client.call('/api.product.favorite/lists', params={'page': 1, 'page_size': 1})
        await database_work(observe, db, account, header)
        report = {'state': batch['state'], 'checked': checked, 'deleted_total': batch['deleted'],
                  'remaining': len(batch['pending']), 'retained': len(batch['retained']), 'errors': errors,
                  'used': header['used'], 'limit': header['limit']}
        await database_work(control.record, db, 'seed', context['owner'], '', time.time(), 'favorite_reclaim', report)
        result.append(report)
    return result


def capacity_recovery_candidate(queue, publication, journal):
    """One-time recovery selection; remote absence and unchanged identities still required."""
    from .favorite_lookup import capacity_rejection
    body = json.loads(queue['body']) if isinstance(queue['body'], str) else queue['body']
    if queue['state'] not in ('quarantined', 'publishing', 'awaiting_remote') or body.get('submitted') or body.get('repair_manual'):
        return False
    if queue['state'] == 'quarantined' and (body.get('isolation') or {}).get('reason') != 'remote_outcome_or_platform_issue':
        return False
    if publication.get('backend') != 'maozi_follow' or publication.get('verified') or publication.get('product'):
        return False
    details = journal['details']
    if (journal['phase'] not in ('favorite_pending','manual_review') or details.get('favorite_acknowledged_at')
            or details.get('favorite_id') or details.get('favorite_attempts') != 1):
        return False
    if journal['phase'] == 'manual_review' and (details.get('reason') != 'reconciliation_timeout'
            or details.get('unresolved_phase') != 'favorite_pending'):
        return False
    events = publication.get('events', [])
    errors = [e for e in events if e.get('error')]
    return (len(errors) == 1 and errors[0].get('from') == 'prepared'
            and errors[0].get('error') == 'ExternalContractError' and capacity_rejection(errors[0].get('reason',''))
            and not any(e.get('to') not in (None,'favorite_pending','manual_review') for e in events))
