"""Read-only local report. Does not initialize a database or expose credentials."""
import argparse
import json
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--data', type=Path, required=True)
args = parser.parse_args()
c = sqlite3.connect((args.data.resolve() / 'flowhub.sqlite3').as_uri() + '?mode=ro', uri=True)
c.row_factory = sqlite3.Row
try:
    states = dict(c.execute('SELECT state,count(*) FROM plugin_pipeline GROUP BY state'))
    isolated = [dict(r) for r in c.execute('''SELECT json_extract(body,'$.isolation.reason') reason,
        count(*) count FROM plugin_pipeline WHERE state='quarantined' GROUP BY 1''')]
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    budget = dict(c.execute('SELECT * FROM queue_isolation_budget').fetchone()) if 'queue_isolation_budget' in tables else None
    print(json.dumps({'states': states, 'isolated_reasons': isolated, 'budget': budget}, ensure_ascii=False, indent=2))
finally:
    c.close()
