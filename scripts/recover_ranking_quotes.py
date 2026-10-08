"""Resume explicit, unreviewed ranking SKUs stopped by the old missing-quote repair."""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.ranking_sources import verified_ranking


def recover(db, owner, skus, *, apply=False):
    if not 1 <= len(skus) <= 100 or any(not str(s).isdigit() for s in skus):
        raise ValueError('explicit list of 1..100 numeric SKUs required')
    now = time.time(); results = []
    c = sqlite3.connect(str(db.path) if apply else f'file:{db.path}?mode=ro', uri=not apply, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        if apply: c.execute('BEGIN IMMEDIATE')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for sku in dict.fromkeys(map(str, skus)):
            row = c.execute('''SELECT q.*,p.body product,w.body workflow FROM plugin_pipeline q
                JOIN sourcing_products p USING(owner,sku,seller)
                JOIN repair_workflows w USING(owner,sku,seller)
                WHERE q.owner=? AND q.sku=?''', (owner, sku)).fetchone()
            if not row:
                results.append({'sku': sku, 'state': 'retained'}); continue
            key = (owner, sku, row['seller']); q = json.loads(row['body']); w = json.loads(row['workflow'])
            p = json.loads(row['product'])
            safe = (row['state'] == 'needs_review' and q.get('submitted') is False
                and q.get('reason') == 'repair_input_required: direct_valuation_facts_unavailable'
                and not any(q.get(k) for k in ('offer_id', 'listing_control_id', 'official_dossier_pending', 'repair_full_dossier'))
                and w.get('state') == 'manual' and w.get('kind') == 'valuation' and w.get('stage') == 'facts'
                and w.get('reason') == 'direct_valuation_facts_unavailable' and 'sale_price' in w.get('missing_fields', [])
                and p.get('coverage') == 'sales-ranking' and verified_ranking(p, now))
            for table in ('plugin_reviews', 'human_reviews', 'product_listing_controls', 'plugin_publications'):
                if table in tables and c.execute(f'SELECT 1 FROM {table} WHERE owner=? AND sku=? AND seller=?', key).fetchone():
                    safe = False
            if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?', key[:2]).fetchone(): safe = False
            if c.execute('SELECT 1 FROM jobs WHERE owner=? AND source_key=?', key[:2]).fetchone(): safe = False
            if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?', (*key, now)).fetchone(): safe = False
            if not safe:
                results.append({'sku': sku, 'state': 'retained'}); continue
            results.append({'sku': sku, 'seller': row['seller'], 'state': 'resumed' if apply else 'eligible'})
            if not apply: continue
            # Archive both original states before changing this machine-generated hold.
            c.execute('INSERT INTO repair_workflow_events(owner,sku,seller,at,body) VALUES(?,?,?,?,?)',
                      (*key, now, json.dumps({'event': 'ranking_quote_recovery', 'previous_queue': dict(row) | {'product': None},
                                             'previous_workflow': w})))
            w.update(state='waiting', next_at=0, reason='ranking_quote_repair_available')
            q.update(repair_workflow=w, reason='ranking_quote_repair_available', updated_at=now)
            q.pop('repair_manual', None); q.pop('repair_reason', None)
            c.execute("UPDATE plugin_pipeline SET state='needs_fields',body=?,due=0 WHERE owner=? AND sku=? AND seller=?",
                      (json.dumps(q), *key))
            c.execute('UPDATE repair_workflows SET body=?,updated=? WHERE owner=? AND sku=? AND seller=?',
                      (json.dumps(w), now, *key))
        if apply: c.commit()
    finally:
        c.close()
    return {'applied': apply, 'at': now, 'tasks': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True); parser.add_argument('--owner', required=True)
    parser.add_argument('--skus-file', required=True); parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(recover(Database.open_existing(args.data), args.owner,
                             json.loads(Path(args.skus_file).read_text()), apply=args.apply), ensure_ascii=False))
