#!/usr/bin/env python3
"""Read-only seed export using exactly the live review cards and revisions."""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from flowhub.manual_reviews import card, load


def export(db, owner):
    # Never initialize a Database or migrate/write the production queue here.
    with sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT sku,seller FROM plugin_pipeline WHERE owner=? AND state='needs_review' ORDER BY sku,seller", (owner,)).fetchall()
        items = [card(conn, owner, load(conn, owner, r['sku'], r['seller']), r['sku'], r['seller']) for r in rows]
    return {'generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()), 'owner':owner, 'total':len(items), 'items':items}


def main():
    owner = os.environ.get('FLOWHUB_REVIEW_OWNER')
    if not owner:raise SystemExit('请明确设置 FLOWHUB_REVIEW_OWNER，不能自动选择其他租户')
    payload = export(os.environ.get('FLOWHUB_REVIEW_DB', ROOT / 'data' / 'flowhub.sqlite3'), owner)
    out = Path(__file__).resolve().parents[1] / 'data' / 'reviews.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output':str(out), 'owner':owner, 'total':payload['total']}, ensure_ascii=False))


if __name__ == '__main__':main()
