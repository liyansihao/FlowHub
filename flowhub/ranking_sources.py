"""Opt-in sales-ranking intake; independent receipts, resumable pages, no publishing."""
import json
import time
from datetime import datetime, timezone

from .source_library import SourceFilters, SourceLibrary, assess, fingerprint, numeric, ranking_product

CONTRACT = 'flowhub-sales-ranking-v1'
MAX_AGE = 7 * 86400


def policy(db, owner):
    path = db.directory / 'ranking-source-policy.json'
    value = json.loads(path.read_text()) if path.exists() else {}
    return value if value.get('enabled') is True and value.get('owner') == owner else None


def timestamp(value):
    number = numeric(value)
    if number is not None:
        return number / 1000 if number > 1e12 else number
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # Provider calendar dates represent their reporting day, not fetch time.
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def fresh_sales(evidence, sku, seller, now):
    count = numeric(evidence.get('sold_count'))
    observed = numeric(evidence.get('observed_at'))
    updated = evidence.get('provider_updated_at')
    provider_at = timestamp(updated) if updated not in (None, '') else observed
    return bool(str(evidence.get('sku')) == str(sku)
                and str(evidence.get('seller_id')) == str(seller)
                and evidence.get('period') in ('28d', 'monthly')
                and count is not None and count > 0
                and observed is not None and 0 <= now - observed < MAX_AGE
                and provider_at is not None and 0 <= now - provider_at < MAX_AGE)


def verified_ranking(product, now=None):
    now = time.time() if now is None else now
    evidence = product.get('ranking_source') or {}
    return bool(evidence.get('contract') == CONTRACT and evidence.get('evidence_hash')
                and evidence.get('endpoint') == '/api.selection.top/lists'
                and evidence.get('period') == '28d'
                and fresh_sales(evidence, product.get('sku'), product.get('seller_id'), now))


def schema(db):
    def initialize():
        SourceLibrary(db)
        with db.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS source_ranking_pages(
              owner TEXT,category TEXT,page INTEGER,due REAL,refresh_due REAL,
              exhausted INTEGER,failures INTEGER,last_hash TEXT,updated REAL,
              PRIMARY KEY(owner,category));
            CREATE TABLE IF NOT EXISTS source_rankings(
              owner TEXT,sku TEXT,seller TEXT,body TEXT,observed REAL,
              PRIMARY KEY(owner,sku,seller));
            CREATE INDEX IF NOT EXISTS source_rankings_recent ON source_rankings(owner,observed,sku,seller);
            CREATE TABLE IF NOT EXISTS source_seed_demand_checks(
              owner TEXT,sku TEXT,seller TEXT,due REAL,failures INTEGER DEFAULT 0,
              PRIMARY KEY(owner,sku,seller));
            CREATE TABLE IF NOT EXISTS source_ranking_activation(owner TEXT PRIMARY KEY,at REAL);
            CREATE TABLE IF NOT EXISTS source_legacy_seeds(
              owner TEXT,shop TEXT,offer TEXT,sku TEXT,PRIMARY KEY(owner,shop,offer));
            ''')
    db.schema_once('ranking_sources', initialize)


def activate(db, owner, now):
    """Snapshot existing roots once, including across restarts and toggle cycles."""
    schema(db)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('INSERT OR IGNORE INTO source_ranking_activation VALUES(?,?)', (owner, now)).rowcount:
            c.execute('INSERT INTO source_legacy_seeds SELECT owner,shop,offer,sku FROM sourcing_seeds WHERE owner=?', (owner,))


def demand_evidence(c, owner, sku, seller, now):
    evidence = []
    row = c.execute('SELECT body FROM source_rankings WHERE owner=? AND sku=? AND seller=?',
                    (owner, sku, seller)).fetchone()
    if row:
        evidence.append(json.loads(row[0]))
    row = c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',
                    (owner, sku, seller)).fetchone()
    if row:
        product = json.loads(row[0])
        detail = product.get('plugin_detail') or {}
        monthly = detail.get('monthly_sales') or {}
        if str(detail.get('sku')) == str(sku) and monthly:
            evidence.append(dict(monthly, sku=sku, seller_id=seller))
    # Do not revive an older positive count after a newer zero/unknown response.
    latest = max(evidence, key=lambda e: numeric(e.get('observed_at')) or 0, default={})
    return latest if fresh_sales(latest, sku, seller, now) else None


def seed_allowed(c, owner, seed, now):
    if c.execute('SELECT 1 FROM source_legacy_seeds WHERE owner=? AND shop=? AND offer=? AND sku=?',
                 (owner, seed['shop'], seed['offer'], seed['sku'])).fetchone():
        return True
    body = json.loads(seed['body'])
    seller = str(body.get('seller_id') or '')
    qualified = body.get('sales_qualification') or {}
    # Qualification is recorded when the seed is added. Existing scans keep running.
    if qualified.get('at') is not None and fresh_sales(qualified.get('evidence') or {}, seed['sku'], seller, qualified['at']):
        return True
    return bool(demand_evidence(c, owner, seed['sku'], seller, now))


def priority_candidates(c, owner, now):
    # Rank only keys; never sort the large source bodies in admission's write lock.
    return c.execute('''SELECT p.id,r.body AS ranking FROM source_rankings r
        JOIN sourcing_products p USING(owner,sku,seller)
        WHERE r.owner=? AND r.observed>? AND r.observed<=?
        AND NOT EXISTS(SELECT 1 FROM pipeline_admissions a WHERE a.owner=p.owner AND a.sku=p.sku)
        AND NOT EXISTS(SELECT 1 FROM plugin_pipeline q WHERE q.owner=p.owner AND q.sku=p.sku)
        AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.owner=p.owner AND j.source_key=p.sku)
        AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=p.owner AND b.source_key=p.sku)
        ORDER BY p.id''', (owner, now - MAX_AGE, now))


def prepare_page(db, owner, config, now):
    with db.connect() as c:
        setting = c.execute('SELECT body,secret FROM sourcing_settings WHERE owner=? AND enabled=1', (owner,)).fetchone()
        if not setting:
            return None
        filters = SourceFilters(**json.loads(setting['body']))
        categories = config.get('categories', filters.categories) or ['']
        for category in categories:
            c.execute('INSERT OR IGNORE INTO source_ranking_pages VALUES(?,?,1,0,0,0,0,NULL,0)', (owner, str(category)))
        task = c.execute('''SELECT * FROM source_ranking_pages WHERE owner=? AND due<=?
            AND category IN (SELECT value FROM json_each(?))
            ORDER BY updated,category LIMIT 1''', (owner, now, json.dumps([str(v) for v in categories]))).fetchone()
        if not task:
            return None
        task = dict(task)
        task['head'] = now >= task['refresh_due']
        task['request_page'] = 1 if task['head'] else task['page']
        if not task['head']:
            count = c.execute('''SELECT COUNT(*) FROM source_rankings r JOIN sourcing_products p USING(owner,sku,seller)
                WHERE r.owner=? AND r.observed>? AND json_extract(r.body,'$.sold_count')>0
                AND NOT EXISTS(SELECT 1 FROM pipeline_admissions a WHERE a.owner=r.owner AND a.sku=r.sku)
                AND NOT EXISTS(SELECT 1 FROM plugin_pipeline q WHERE q.owner=r.owner AND q.sku=r.sku)
                AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.owner=r.owner AND j.source_key=r.sku)
                AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=r.owner AND b.source_key=r.sku)''', (owner, now - MAX_AGE)).fetchone()[0]
            if count >= config.get('max_pending', 2000):
                c.execute('UPDATE source_ranking_pages SET due=refresh_due WHERE owner=? AND category=?', (owner, task['category']))
                return None
        return task, filters, db.open(setting['secret'])['erp_token']


def commit_page(db, owner, task, rows, last, query, blocks, now):
    library = SourceLibrary(db)
    page = task['request_page']
    digest = fingerprint(rows)
    if rows and not task['head'] and digest == task['last_hash']:
        raise ValueError('repeated_ranking_page')
    products = [ranking_product(row, now) for row in rows]
    if task['category'] and any(p['category_id'] != task['category'] for p in products):
        raise ValueError('ranking_category_mismatch')
    added = 0
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        from .pipeline_modules.control import paused
        if not policy(db, owner) or paused(db, 'seed'):
            return {'state': 'paused'}
        setting=c.execute('SELECT body FROM sourcing_settings WHERE owner=?',(owner,)).fetchone()
        filters=SourceFilters(**json.loads(setting[0])) if setting else SourceFilters()
        for row, product in zip(rows, products):
            sku, seller = product['sku'], product['seller_id']
            if not seller:
                continue
            evidence = {'contract': CONTRACT, 'endpoint': '/api.selection.top/lists',
                        'sku': sku, 'seller_id': seller, 'period': '28d',
                        'sold_count': product['sold_count_28d'], 'observed_at': now,
                        'provider_updated_at': product['provider_updated_at'],
                        'category_id': product['category_id'], 'query': query,
                        'evidence_hash': fingerprint({'query': query, 'row': row})}
            c.execute('INSERT OR IGNORE INTO sourcing_evidence VALUES(?,?,?,?,?)',
                      (owner, evidence['evidence_hash'], sku, json.dumps({'ranking': evidence, 'row': row}), now))
            # Save zero/missing/latest evidence too: old positive sales must not stay preferred.
            c.execute('INSERT INTO source_rankings VALUES(?,?,?,?,?) ON CONFLICT(owner,sku,seller) DO UPDATE SET body=excluded.body,observed=excluded.observed',
                      (owner, sku, seller, json.dumps(evidence), now))
            if (not fresh_sales(evidence, sku, seller, now) or sku in blocks['skus']
                    or assess(product, filters, now)['failed']):
                continue
            if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?', (owner, sku)).fetchone():
                continue
            # Refresh only the independent demand proof. Do not touch captured price,
            # facts, review or queue; slow repairs must be able to see renewed evidence.
            if c.execute('SELECT 1 FROM sourcing_products WHERE owner=? AND sku=? AND seller=?', (owner, sku, seller)).fetchone():
                c.execute("UPDATE sourcing_products SET body=json_set(body,'$.ranking_source',json(?)) WHERE owner=? AND sku=? AND seller=? AND json_extract(body,'$.coverage')='sales-ranking'",
                          (json.dumps(evidence), owner, sku, seller))
                continue
            product.update(coverage='sales-ranking', ranking_source=evidence)
            added += library.put(owner, product, {'channel': 'sales-ranking', 'query': query}, connection=c)
        ended = not rows or (last is not None and page >= last)
        refresh_due = now + 21600 if task['head'] else task['refresh_due']
        next_page = (2 if task['exhausted'] or task['page'] == 1 else task['page']) if task['head'] else page + 1
        exhausted = ended
        due = refresh_due if exhausted else min(now + 30, refresh_due)
        c.execute('UPDATE source_ranking_pages SET page=?,due=?,refresh_due=?,exhausted=?,failures=0,last_hash=?,updated=? WHERE owner=? AND category=?',
                  (next_page, due, refresh_due, int(exhausted), digest, now, owner, task['category']))
    return {'state': 'ranking_refreshed', 'page': page, 'added': added, 'category': task['category']}


async def refresh_one(db, owner, now=None, request=None, delists=None):
    from .pipeline_modules.database_work import run as database_work
    from .source_acquisition import erp_request, page_data, ranking_query
    from .source_delists import read_delists
    now = time.time() if now is None else now
    config = policy(db, owner)
    if not config:
        return {'state': 'ranking_disabled'}
    await database_work(activate, db, owner, now)
    prepared = await database_work(prepare_page, db, owner, config, now)
    if not prepared:
        return {'state': 'no_due_ranking'}
    task, filters, token = prepared
    query = ranking_query({'mainType': 'china', **({'category2': task['category']} if task['category'] else {})}, task['request_page'], filters)
    try:
        blocks = await (delists or read_delists)()
        data = await (request or erp_request)('/api.selection.top/lists', query, token)
        rows, last = page_data(data, task['request_page'], 100)
        return await database_work(commit_page, db, owner, task, rows, last, query, blocks, now)
    except Exception as error:
        def failed():
            with db.connect() as c:
                c.execute('UPDATE source_ranking_pages SET failures=failures+1,due=?,updated=? WHERE owner=? AND category=?',
                          (now + min(3600, 60 * 2 ** min(task['failures'], 6)), now, owner, task['category']))
        await database_work(failed)
        return {'state': 'ranking_retry', 'reason': type(error).__name__}


async def verify_seed_one(db, owner, now=None, request=None):
    """A published expansion SKU still needs its own sales; never inherit root sales."""
    from .pipeline_modules.database_work import run as database_work
    from .source_acquisition import erp_request, page_data
    now = time.time() if now is None else now
    if not policy(db, owner):
        return {'state': 'ranking_disabled'}
    def select():
        with db.connect() as c:
            row = c.execute('SELECT * FROM source_seed_demand_checks WHERE owner=? AND due<=? ORDER BY due,sku LIMIT 1', (owner, now)).fetchone()
            setting = c.execute('SELECT secret FROM sourcing_settings WHERE owner=? AND enabled=1', (owner,)).fetchone()
            return (dict(row), db.open(setting[0])['erp_token']) if row and setting else None
    selected = await database_work(select)
    if not selected:
        return {'state': 'no_due_seed_sales'}
    task, token = selected
    key = (owner, task['sku'], task['seller'])
    query = {'sku': task['sku'], 'page': 1, 'page_size': 100}
    try:
        data = await (request or erp_request)('/api.selection.top/lists', query, token)
        rows, _ = page_data(data, 1, 100)
        if any(str(row.get('sku')) != task['sku'] for row in rows):
            raise ValueError('seed_sales_identity_mismatch')
        matches = [r for r in rows if str(r.get('seller_id')) == task['seller']]
        if len(matches) > 1:
            raise ValueError('seed_sales_ambiguous')
        row = matches[0] if matches else {}
        evidence = {'contract': CONTRACT, 'endpoint': '/api.selection.top/lists',
                    'sku': task['sku'], 'seller_id': task['seller'], 'period': '28d',
                    'sold_count': numeric(row.get('sold_count')), 'observed_at': now,
                    'provider_updated_at': row.get('update_time'), 'query': query,
                    'evidence_hash': fingerprint({'query': query, 'row': row})}
        def save():
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT INTO source_rankings VALUES(?,?,?,?,?) ON CONFLICT(owner,sku,seller) DO UPDATE SET body=excluded.body,observed=excluded.observed',
                          (*key, json.dumps(evidence), now))
                c.execute('UPDATE source_seed_demand_checks SET due=?,failures=0 WHERE owner=? AND sku=? AND seller=?', (now + 21600, *key))
        await database_work(save)
        return {'state': 'seed_sales_checked', 'sku': task['sku'], 'qualified': fresh_sales(evidence, task['sku'], task['seller'], now)}
    except Exception as error:
        def failed():
            with db.connect() as c:
                c.execute('UPDATE source_seed_demand_checks SET failures=failures+1,due=? WHERE owner=? AND sku=? AND seller=?',
                          (now + min(21600, 300 * 2 ** min(task['failures'], 7)), *key))
        await database_work(failed)
        return {'state': 'seed_sales_retry', 'reason': type(error).__name__}
