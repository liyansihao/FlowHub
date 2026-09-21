"""Durable queue isolation. Remote inspection is strictly read-only and request-budgeted."""
import json
import asyncio
from .database_work import run as database_work
import secrets
import time
from dataclasses import asdict

import httpx

POLICY_FILE = 'queue-isolation.json'
READ_PATHS = {'/v2/warehouse/list', '/v3/product/info/list', '/v2/product/info/stocks-by-warehouse/fbs'}


def enabled(db):
    path = db.directory / POLICY_FILE
    return path.exists() and json.loads(path.read_text()).get('enabled') is True


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS queue_isolation_receipts(
        id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,at REAL,reason TEXT,
        previous_state TEXT,previous_body TEXT,previous_due REAL,previous_attempts INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS queue_isolation_budget(
        id INTEGER PRIMARY KEY CHECK(id=1),baseline INTEGER,used INTEGER,last_scan REAL)''')
    total = total_calls(c)
    c.execute('INSERT OR IGNORE INTO queue_isolation_budget(id,baseline,used,last_scan) VALUES(1,?,0,0)', (total,))
    columns = {r[1] for r in c.execute('PRAGMA table_info(queue_isolation_budget)')}
    for name in ('idle_used', 'idle_window', 'idle_window_used'):
        if name not in columns:
            c.execute(f'ALTER TABLE queue_isolation_budget ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')


def total_calls(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='official_api_metrics'").fetchone():
        return 0
    return c.execute('SELECT COALESCE(SUM(calls),0) FROM official_api_metrics').fetchone()[0]


def classify(state, body, now):
    if body.get('isolation_recovery_until', 0) > now:
        return None
    if state == 'needs_fields':
        if body.get('repair_workflow'):
            # Stage failures and dependency waits have distinct durable counters.
            # Do not quarantine a restarted staged task using legacy attempt totals.
            return None
        retry = body.get('repair_retry') or {}
        count = max(retry.get('total_attempts', 0), retry.get('attempts', 0))
        since = body.get('repair_wait_started_at', body.get('updated_at', body.get('requested_at', now)))
        if count >= 3:
            return 'three_failed_repairs'
        if now - since >= 7200:
            return 'repair_wait_over_two_hours'
    if state in ('awaiting_remote', 'publishing'):
        if body.get('reason') == 'favorite_visibility_exhausted':
            return 'favorite_visibility_exhausted'
        if body.get('phase') == 'manual_review':
            return 'remote_outcome_or_platform_issue'
        if body.get('phase') in ('submitting', 'reconciling', 'sync_pending', 'stock_pending'):
            since = (body.get('readback_schedule') or {}).get('first_wait_at', body.get('requested_at', now))
            if now - since >= 7200:
                return 'remote_wait_over_two_hours'
    return None


def cleanup(db, *, now=None):
    if not enabled(db):
        return {'isolated': 0}
    now = time.time() if now is None else now
    count = 0
    with db.write_transaction() as c:
        schema(c)
        paused = {r['module'] for r in c.execute('SELECT module FROM pipeline_module_control WHERE paused=1')}
        rows = c.execute('''SELECT q.* FROM plugin_pipeline q JOIN users u ON u.id=q.owner
          LEFT JOIN plugin_pipeline_leases l USING(owner,sku,seller)
          WHERE u.active=1 AND q.state IN ('needs_fields','awaiting_remote','publishing')
          AND (l.expires IS NULL OR l.expires<=?)''', (now,)).fetchall()
        for row in rows:
            if ('seed' if row['state'] == 'needs_fields' else 'publication') in paused:
                continue
            body = json.loads(row['body'])
            reason = classify(row['state'], body, now)
            if not reason and row['state']=='needs_fields' and not body.get('repair_workflow'):
                from .lifecycle import config as lifecycle_config, reason as lifecycle_reason
                reason=lifecycle_reason(body,now,lifecycle_config(db))
            if not reason:
                continue
            c.execute('INSERT INTO queue_isolation_receipts VALUES(NULL,?,?,?,?,?,?,?,?,?)',
                      (row['owner'], row['sku'], row['seller'], now, reason, row['state'],
                       row['body'], row['due'], row['attempts']))
            body['isolation'] = {'at': now, 'reason': reason, 'previous_state': row['state'],
                                 'checks': 0, 'remote_writes_allowed': False}
            body['updated_at'] = now
            c.execute("UPDATE plugin_pipeline SET state='quarantined',body=?,due=? WHERE owner=? AND sku=? AND seller=?",
                      (json.dumps(body), now, row['owner'], row['sku'], row['seller']))
            count += 1
    return {'isolated': count}


class BudgetDeferred(Exception):
    pass


class ReadBudgetTransport(httpx.AsyncBaseTransport):
    """Inner transport sees only actual dispatches, excluding metadata-cache hits."""
    def __init__(self, db, inner=None):
        self.db = db
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if (request.method != 'POST' or request.url.host != 'api-seller.ozon.ru'
                or request.url.scheme != 'https' or request.url.path not in READ_PATHS):
            raise ValueError('quarantine_remote_write_forbidden')
        await database_work(self.reserve)
        return await self.inner.handle_async_request(request)

    def reserve(self):
        with self.db.write_transaction() as c:
            row = c.execute('SELECT * FROM queue_isolation_budget WHERE id=1').fetchone()
            if row is None:
                raise BudgetDeferred('budget_not_initialized')
            # Budget is conservative: reserved but failed/deferred attempts also consume it.
            normal = max(0, total_calls(c) - row['baseline'] - row['used'] - row['idle_used'])
            busy = c.execute("SELECT COUNT(*) FROM plugin_pipeline WHERE state IN ('queued','evaluating','publishing') AND due<=?", (time.time(),)).fetchone()[0] >= 8
            divisor = 19 if busy else 9  # <=5% under pressure, <=10% otherwise.
            foreground = c.execute("SELECT COUNT(*) FROM plugin_pipeline WHERE state IN ('queued','evaluating','publishing')").fetchone()[0]
            if foreground == 0:
                window = int(time.time() // 60)
                used = row['idle_window_used'] if row['idle_window'] == window else 0
                if used >= 3:
                    raise BudgetDeferred('idle_read_limit')
                c.execute('UPDATE queue_isolation_budget SET idle_used=idle_used+1,idle_window=?,idle_window_used=? WHERE id=1', (window, used+1))
            else:
                if row['used'] + 1 > normal // divisor:
                    raise BudgetDeferred('foreground_request_budget_required')
                c.execute('UPDATE queue_isolation_budget SET used=used+1 WHERE id=1')

    async def aclose(self):
        await self.inner.aclose()


async def inspect(db, row):
    """Never invokes publication advance, favorite recovery or a write endpoint."""
    from ..official_api import client
    from ..official_status import OzonSellerStatusAdapter
    key = (row['owner'], row['sku'], row['seller'])
    with db.connect() as c:
        prior = c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?', key).fetchone()
        if not prior:
            return {'result': 'manual_dossier_repair_required'}
        record = json.loads(prior[0])
        store = c.execute('SELECT * FROM stores WHERE owner=? AND id=? AND verified=1',
                          (row['owner'], record['store_id'])).fetchone()
        if store is None:
            return {'result': 'verified_store_required'}
    cfg = json.loads(store['config']); keys = db.open(store['secret'])
    plan = record.get('plan') or {}
    if (str(plan.get('shop_id')) != str(cfg['shop_id'])
            or str(plan.get('warehouse_id')) != str(cfg['warehouse_id'])
            or str(plan.get('sku')) != row['sku']):
        return {'result': 'original_plan_binding_changed'}
    if not keys.get('client_id') or not keys.get('api_key'):
        return {'result': 'official_read_credentials_required'}
    async with client(db, keys, transport=ReadBudgetTransport(db)) as api:
        port = OzonSellerStatusAdapter(api, shop_id=str(cfg['shop_id']), warehouse_id=str(cfg['warehouse_id']))
        await port.verify_identity()
        product = await port.find_product(str(cfg['shop_id']), record['offer_id'])
        if not product:
            return {'result': 'original_offer_not_visible'}
        if product.status == 'selling' and product.sku.isdigit() and int(product.sku) > 0:
            stocks = await port.read_stocks(product)
            if any(s.warehouse_id == str(cfg['warehouse_id']) and s.present == 99 for s in stocks):
                return {'result': 'selling_stock_confirmed', 'verified': True,
                        'product': asdict(product), 'stocks': [asdict(v) for v in stocks],
                        'publication_body': prior[0]}
        # This is a one-time return to the original guarded workflow, NOT a stock write.
        # Unknown submitting/stock writes are kept quarantined; never reset their intents.
        from flowef.adapters.persistence.test_listing_journal import TestListingJournal
        journal = TestListingJournal(db.directory / 'plugin-production.sqlite3').read(record['offer_id'])
        if (product.status == 'ready_to_sell' and not product.issue_codes
                and journal['phase'] == 'manual_review'
                and journal['details'].get('reason') == 'reconciliation_timeout'
                and journal['details'].get('unresolved_phase') in ('reconciling', 'sync_pending')
                and record.get('write_deadline', 0) > time.time()):
            return {'result': 'original_offer_ready_for_guarded_recovery', 'resume': True}
        return {'result': 'original_offer_requires_review', 'status': product.status,
                'issues': list(product.issue_codes)}


def claim_inspection(db):
    if not enabled(db):
        return False
    now = time.time(); token = secrets.token_hex(16)
    with db.write_transaction() as c:
        schema(c)
        if c.execute("SELECT 1 FROM pipeline_module_control WHERE module='publication' AND paused=1").fetchone():
            return False
        budget = c.execute('SELECT * FROM queue_isolation_budget WHERE id=1').fetchone()
        if now - budget['last_scan'] < 60:
            return False
        row = c.execute('''SELECT q.* FROM plugin_pipeline q JOIN users u ON u.id=q.owner
          LEFT JOIN plugin_pipeline_leases l USING(owner,sku,seller)
          WHERE u.active=1 AND q.state='quarantined' AND q.due<=?
          AND (l.expires IS NULL OR l.expires<=?)
          ORDER BY CASE WHEN json_extract(q.body,'$.isolation.previous_state')='needs_fields' THEN 2
            WHEN json_extract(q.body,'$.reason')='favorite_visibility_exhausted' THEN 1 ELSE 0 END, q.due LIMIT 1''', (now, now)).fetchone()
        if row is None:
            return False
        key = (row['owner'], row['sku'], row['seller'])
        c.execute('UPDATE queue_isolation_budget SET last_scan=? WHERE id=1', (now,))
        c.execute('INSERT OR REPLACE INTO plugin_pipeline_leases VALUES(?,?,?,?,?)', (*key, token, now+120))
        row = dict(row)
    return row,key,token


async def tick(db):
    from ..plugin_pipeline import release_lease
    task=asyncio.create_task(asyncio.to_thread(claim_inspection,db))
    try:claimed=await asyncio.shield(task)
    except asyncio.CancelledError:
        claimed=await task
        if claimed:await database_work(release_lease,db,claimed[1],claimed[2])
        raise
    if not claimed:return False
    row,key,token=claimed

    from ..source_library import SourceLibrary
    try:
        library = SourceLibrary(db)
        try:
            result = await asyncio.wait_for(inspect(db, row), timeout=45)
        except Exception as error:
            result = {'result': 'read_deferred', 'error_type': type(error).__name__}
        return await database_work(finish_inspection,db,key,token,result,library)
    finally:
        await database_work(release_lease,db,key,token)

def finish_inspection(db,key,token,result,library):
    with db.write_transaction() as c:
        current = c.execute('SELECT * FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?', key).fetchone()
        lease = c.execute('SELECT token FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=?', key).fetchone()
        if not current or current['state'] != 'quarantined' or not lease or lease[0] != token:
            return False
        body = json.loads(current['body']); isolation = body['isolation']
        isolation.update(last_checked_at=time.time(), last_result={k:v for k,v in result.items() if k != 'publication_body'},
                         checks=isolation.get('checks', 0)+1)
        state = 'quarantined'; due = time.time()+21600
        if result.get('error_type') in ('BudgetDeferred', 'OfficialDeferred'):
            due = time.time()+900
        if result.get('verified') and close_verified(c, db, key, result, body, library):
            state = 'selling'; due = time.time()
        elif result.get('resume') and not body.get('isolation_recovery_attempted'):
            body['isolation_recovery_attempted'] = True
            body['isolation_recovery_until'] = time.time()+1800
            body.setdefault('isolation_history', []).append(body.pop('isolation'))
            state = 'awaiting_remote'; due = time.time()
        body['updated_at'] = time.time()
        c.execute('UPDATE plugin_pipeline SET state=?,body=?,due=? WHERE owner=? AND sku=? AND seller=?',
                  (state, json.dumps(body), due, *key))
    return True



def close_verified(c, db, key, result, body, library):
    """Close the ORIGINAL local journal after fresh selling+stock evidence; no remote writes."""
    original = result.get('publication_body')
    current = c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?', key).fetchone()
    if not current or current[0] != original:
        return False
    record = json.loads(original); plan = record['plan']; product = result['product']
    if (plan.get('purpose') != 'production' or plan.get('stock_target') != 99
            or product['offer_id'] != record['offer_id'] or product['shop_id'] != str(plan['shop_id'])
            or str(plan['sku']) != key[1] or product['status'] != 'selling'
            or not any(str(v['warehouse_id']) == str(plan['warehouse_id']) and v['present'] == 99
                       for v in result['stocks'])):
        return False
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='product_listing_controls'").fetchone():
        if c.execute("SELECT 1 FROM product_listing_controls WHERE owner=? AND sku=? AND seller=? AND action='unlist' AND state IN ('queued','running','waiting','hold_queued','hold_waiting')", key).fetchone():
            return False
    journal_path = db.directory / 'plugin-production.sqlite3'
    if not journal_path.exists():
        return False
    c.execute('ATTACH DATABASE ? AS isolation_journal', (str(journal_path),))
    journal = c.execute('SELECT plan,phase FROM isolation_journal.zero_stock_tests WHERE offer_id=?', (record['offer_id'],)).fetchone()
    if not journal or json.loads(journal['plan']) != plan or journal['phase'] not in (
            'manual_review','stock_pending','sync_pending','reconciling','submitting','stock_verified'):
        return False
    now = time.time()
    c.execute("UPDATE isolation_journal.zero_stock_tests SET phase='stock_verified',details=json_patch(details,?),updated_at=CURRENT_TIMESTAMP WHERE offer_id=? AND phase=?",
              (json.dumps({'reason':None,'isolation_verified_at':now}),record['offer_id'],journal['phase']))
    if record.get('phase') != 'stock_verified':
        record.setdefault('events', []).append({'at':now,'from':record.get('phase'),'to':'stock_verified','source':'isolated_readback'})
    record.update(phase='stock_verified', verified=True, product=product, stocks=result['stocks'])
    record.setdefault('first_verified_at', now)
    c.execute('UPDATE plugin_publications SET body=?,updated=? WHERE owner=? AND sku=? AND seller=?',
              (json.dumps(record),now,*key))
    isolation = body.pop('isolation'); isolation['completed_at'] = now
    body.setdefault('isolation_history', []).append(isolation)
    body.update(phase='stock_verified', verified=True, product=product, offer_id=record['offer_id'])
    body.pop('error',None);body.pop('reason',None)
    source = c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    if source:
        p=json.loads(source[0]);p.setdefault('listing_review',{}).update(
            observed_at=now,label='已回查可售，库存99',publication_ready=True,
            publication={'shop_id':plan['shop_id'],'offer_id':record['offer_id'],'state':'stock_verified',
                         'product':product,'stocks':result['stocks']},
            pipeline={'state':'selling',**body})
        library.put(key[0],p,{'channel':'isolated-readback','offer_id':record['offer_id']},connection=c)
    wf = c.execute('SELECT modules FROM workflows WHERE owner=?',(key[0],)).fetchone()
    modules = {k:dict(c.execute('SELECT * FROM modules WHERE id=?',(v,)).fetchone())
               for k,v in json.loads(wf[0]).items()} if wf else {}
    review=record.get('review',{})
    c.execute('INSERT OR IGNORE INTO jobs(id,owner,source_key,store_id,phase,data,modules,next_at,created,updated,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
              (record['offer_id'],key[0],key[1],record['store_id'],'selling',
               json.dumps({'candidate':review.get('candidate'),'match':review.get('result'),
                           'external_publication':record,'product_id':product['product_id']}),
               json.dumps(modules),0,record.get('started_at',now),now,'隔离回查确认可售，库存99'))
    return True
