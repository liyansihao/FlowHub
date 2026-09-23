"""Fail-closed favorite dependency ledger. No remote mutation capability.

The production observer has incomplete lifecycle coverage and NEVER certifies
release. Explicit release certificates are reserved for audited producers; a
missing/stale/conflicting observation always withdraws a shadow candidate.
"""
import json
from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS favorite_entities(
              identity TEXT PRIMARY KEY, account TEXT NOT NULL, favorite_id TEXT NOT NULL,
              sku TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 1, classification TEXT NOT NULL,
              version INTEGER NOT NULL, observed REAL NOT NULL, proof TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS favorite_dependency_events(
              id INTEGER PRIMARY KEY, identity TEXT NOT NULL, at REAL NOT NULL,
              version INTEGER NOT NULL, proof TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS favorite_shadow_queue(
              identity TEXT PRIMARY KEY, version INTEGER NOT NULL, eligible_at REAL NOT NULL,
              expires REAL NOT NULL, proof TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def observe(self, account, fid, sku, version, consumers, *, business_version,
                coverage_complete=False, durable_snapshot=False, independent_import=False,
                now=None, ttl=60):
        now = time.time() if now is None else now
        identity = json.dumps([account, str(fid), str(sku)], separators=(',', ':'))
        proof = dict(consumers=consumers, business_version=business_version,
                     coverage_complete=coverage_complete, durable_snapshot=durable_snapshot,
                     independent_import=independent_import)
        active = any(x.get('state') == 'required' for x in consumers)
        complete = bool(consumers) and all(
            x.get('state') == 'released' and x.get('released_at') is not None
            and x.get('evidence') and x.get('consumer') and x.get('business_version')
            for x in consumers)
        safe = bool(business_version and coverage_complete and durable_snapshot
                    and independent_import and complete and not active)
        status = 'required' if active else ('released' if safe else 'unknown')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM favorite_entities WHERE identity=?', (identity,)).fetchone()
            encoded = json.dumps(proof, sort_keys=True)
            if old and (version < old['version'] or (version == old['version'] and encoded != old['proof'])):
                # Contradictory or out-of-order source data is not a new certificate.
                c.execute('DELETE FROM favorite_shadow_queue WHERE identity=?', (identity,))
                c.execute("UPDATE favorite_entities SET required=1,classification='unknown' WHERE identity=?", (identity,))
                return 'unknown'
            c.execute('INSERT OR REPLACE INTO favorite_entities VALUES(?,?,?,?,?,?,?,?,?)',
                      (identity, account, str(fid), str(sku), int(not safe), status, version, now, encoded))
            if not old or old['proof'] != encoded or old['version'] != version:
                c.execute('INSERT INTO favorite_dependency_events(identity,at,version,proof) VALUES(?,?,?,?)',
                          (identity, now, version, encoded))
            if safe:
                prior = c.execute('SELECT eligible_at FROM favorite_shadow_queue WHERE identity=?', (identity,)).fetchone()
                c.execute('INSERT OR REPLACE INTO favorite_shadow_queue VALUES(?,?,?,?,?)',
                          (identity, version, prior[0] if prior else now, now+ttl, encoded))
            else:
                c.execute('DELETE FROM favorite_shadow_queue WHERE identity=?', (identity,))
        return status

    def candidates(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as c:
            return [dict(x) for x in c.execute('''SELECT q.* FROM favorite_shadow_queue q
                JOIN favorite_entities e USING(identity)
                WHERE e.required=0 AND q.version=e.version AND q.expires>?''', (now,))]


async def inventory(get_page, *, max_pages=50):
    """Positive observations only. Stable totals do not prove stable membership."""
    found = {}; totals = []; capacity = None
    for page in range(1, max_pages+1):
        response = await get_page(page)
        rows = response.get('data'); total = response.get('total')
        if not isinstance(rows, list) or isinstance(total, bool) or not str(total).isdigit():
            raise ValueError('invalid favorite page')
        totals.append(int(total)); capacity = response.get('limit', capacity)
        for row in rows:
            if not isinstance(row, dict) or not str(row.get('id', '')).isdigit() or not row.get('sku'):
                raise ValueError('invalid favorite identity')
            fid = str(row['id']); sku = str(row['sku'])
            if fid in found and found[fid] != sku:
                raise ValueError('favorite identity conflict')
            found[fid] = sku
        if len(rows) < 100:
            return found, dict(reported_total=totals[-1], capacity=capacity,
                               observed_unique=len(found), pages=page,
                               enumeration_consistent=len(set(totals)) == 1 and len(found) == totals[-1],
                               authoritative_snapshot=False)
    raise ValueError('favorite pagination bound exceeded')


def record_business_state(c, consumer, stage, business_version, *, favorite_id=None):
    """Called in the caller's transaction: observation only, never release proof.

    No executescript/commit, remote calls, or credentials. The source table is
    additive and can be ignored on rollback. Full producer coverage is not yet
    certified, so these observations cannot make a real favorite deletable.
    """
    c.execute('''CREATE TABLE IF NOT EXISTS favorite_dependency_observations(
        id INTEGER PRIMARY KEY,consumer TEXT NOT NULL,stage TEXT NOT NULL,
        business_version TEXT NOT NULL,favorite_id TEXT,observed REAL NOT NULL)''')
    c.execute('INSERT INTO favorite_dependency_observations(consumer,stage,business_version,favorite_id,observed) VALUES(?,?,?,?,?)',
              (consumer,stage,str(business_version),str(favorite_id) if favorite_id else None,time.time()))
