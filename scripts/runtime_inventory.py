"""Read-only runtime versions; unreported clients remain explicitly unknown."""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


def inventory(directory, now=None):
    now = time.time() if now is None else now
    directory = Path(directory)
    result = {'captured_at': now, 'local': [], 'devices': []}
    result['database_schemas'] = {}
    for name in ('flowhub.sqlite3', 'plugin-production.sqlite3', 'cluster/cluster.sqlite3'):
        dbpath = directory / name
        if not dbpath.exists():
            continue
        with sqlite3.connect(dbpath.resolve().as_uri() + '?mode=ro', uri=True) as db:
            schema = db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
            result['database_schemas'][name] = {
                'user_version': db.execute('PRAGMA user_version').fetchone()[0],
                'schema_sha256': hashlib.sha256(json.dumps(schema, separators=(',', ':')).encode()).hexdigest()}
    for folder in (directory, directory / 'cluster'):
        for path in sorted(folder.glob('runtime-*.json')):
            body = json.loads(path.read_text())
            identity = body['identity']
            try:
                os.kill(identity['pid'], 0)
                pid_exists = True
            except OSError:
                pid_exists = False
            result['local'].append({**body, 'pid_exists_not_identity_proof': pid_exists})
    path = directory / 'cluster/cluster.sqlite3'
    if not path.exists():
        return result
    c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for device in c.execute('SELECT id,name,enabled,last_seen FROM devices WHERE enabled=1'):
            reports = []
            if 'runtime_reports' in tables:
                for r in c.execute('SELECT body,seen FROM runtime_reports WHERE device=? ORDER BY seen DESC', (device['id'],)):
                    reports.append({'identity': json.loads(r['body']), 'seen': r['seen'],
                                    'fresh': 0 <= now-r['seen'] < 90})
            expected = ('erp-agent', 'full-agent', 'compute-worker')
            counts = {name: sum(r['fresh'] and r['identity']['component'] == name for r in reports) for name in expected}
            result['devices'].append({'name': device['name'], 'last_seen': device['last_seen'],
                'version_status': 'reported' if all(counts[k] == 1 for k in expected) else 'missing_stale_or_multiple',
                'fresh_component_counts': counts, 'reports': reports})
    finally:
        c.close()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inventory(args.data.resolve()), ensure_ascii=False, indent=2))
