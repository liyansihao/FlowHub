"""Bounded, read-only queries; no production schema initialization or migrations."""
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def key_for(owner, sku, seller):
    return hashlib.sha256(json.dumps([owner, sku, seller], ensure_ascii=False).encode()).hexdigest()


def safe_label(value):
    # Never export exception text, source URLs, credentials or arbitrary log lines.
    value = str(value or '')
    return value if re.fullmatch(r'[\w.:-]{1,80}', value, re.ASCII) else 'unclassified'


def classify(text):
    text = str(text).lower()
    if any(x in text for x in ('database is locked', 'sqlite_busy', 'sqlite_locked')): return 'sqlite_lock'
    if any(x in text for x in ('disk is full', 'sqlite_full', 'disk i/o', 'cantopen', 'no space left')): return 'storage'
    if any(x in text for x in ('timeout', 'network', 'external', 'httpstatus', 'connection')): return 'external_or_transport'
    return 'application'


class Reader:
    def __init__(self, data, store, clock=time.time, page_size=250):
        self.data, self.store, self.clock = Path(data), store, clock
        self.page_size = page_size
        if self.store.get('observation_started') is None: self.store.put('observation_started', clock())

    @contextmanager
    def connection(self, path=None):
        c = sqlite3.connect((path or (self.data / 'flowhub.sqlite3')).resolve().as_uri()+'?mode=ro', uri=True, timeout=.25)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        deadline = time.monotonic()+2
        c.set_progress_handler(lambda: int(time.monotonic()>deadline), 2000)
        try: yield c
        finally: c.close()

    def query(self, sql, args=()):
        with self.connection() as c: return [dict(r) for r in c.execute(sql, args)]

    def controls(self):
        return {r['module']: {'paused': bool(r['paused']), 'updated': r['updated']}
                for r in self.query('SELECT module,paused,updated FROM pipeline_module_control')}

    def health(self):
        now = self.clock()
        rows = self.query("SELECT pid,heartbeat FROM health WHERE name='worker'")
        try: identity = json.loads((self.data/'runtime-worker.json').read_text())['identity']
        except (OSError, ValueError, KeyError): identity = {}
        beat = rows[0] if rows else {}
        pid = identity.get('pid')
        alive = False
        if isinstance(pid, int) and pid > 1:
            try: os.kill(pid, 0); alive = True
            except (OSError, ProcessLookupError): pass
        matches = pid == beat.get('pid')
        fresh = bool(matches and now - beat.get('heartbeat', 0) < 180)
        controls = self.controls()
        return {'instance_id': identity.get('instance_id'), 'source_revision': identity.get('source_revision'),
                'source_sha256': identity.get('source_sha256'), 'pid': pid, 'pid_alive': alive,
                'heartbeat_at': beat.get('heartbeat'), 'heartbeat_fresh': fresh,
                'state': ('paused_new' if controls.get('seed', {}).get('paused') else 'running') if alive and fresh else ('unresponsive' if alive else 'stopped'),
                'task_liveness': 'not_fully_observable', 'controls': controls,
                'observed_at': now}

    def activity(self):
        return self.query('''SELECT p.owner,p.sku,p.seller,p.state,p.attempts,p.due,l.expires AS lease_expires
                FROM plugin_pipeline_leases l JOIN plugin_pipeline p USING(owner,sku,seller)
                WHERE l.expires>? ORDER BY l.expires LIMIT 200''', (self.clock(),))

    def project(self):
        # Cyclic rowid pages avoid an unindexed full JSON scan or a long read transaction.
        for table, kind in [('plugin_publications', 'publication'), ('plugin_pipeline', 'product')]:
            cursor = self.store.get(table+':cursor', 0)
            cols = "rowid,owner,sku,seller,updated,json_extract(body,'$.backend') backend,json_extract(body,'$.verified') verified,json_extract(body,'$.offer_id') offer_id,json_extract(body,'$.started_at') started_at,json_extract(body,'$.events') events" if kind=='publication' else 'rowid,owner,sku,seller,state,due,attempts'
            rows = self.query(f'SELECT {cols} FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?', (cursor, self.page_size))
            last_rowid = rows[-1]['rowid'] if rows else cursor
            for r in rows:
                k = key_for(r['owner'], r['sku'], r['seller'])
                r.pop('rowid', None)
                r['observed_at'] = self.clock()
                if kind == 'publication':
                    events = json.loads(r.pop('events') or '[]')
                    stamps = [e['at'] for e in events if e.get('to')=='stock_verified' and type(e.get('at')) in (int,float)]
                    r['first_verified_at'] = min(stamps) if stamps and r['verified']==1 else None
                    # Retain first-success facts even after delisting or a later readback change.
                    if r['first_verified_at'] is not None:
                        fact_key = hashlib.sha256(json.dumps([k, r['backend'], r['offer_id'], r['started_at']]).encode()).hexdigest()
                        self.store.save('success', fact_key, r)
                if kind == 'product':
                    previous = self.store.entity(kind,k)
                    if previous and previous['state'] != r['state'] and r['state'] in ('failed','rejected','quarantined'):
                        transition_key = hashlib.sha256(json.dumps([k, previous['state'], previous['observed_at'], r['state'], r['attempts']]).encode()).hexdigest()
                        self.store.save('failure', transition_key, {**r,'transition_observed_at':self.clock(),'previous_observed_at':previous['observed_at'],'category':r['state']})
                self.store.save(kind, k, r)
            if len(rows) < self.page_size:
                self.store.put(table+':cursor', 0)
                self.store.put(table+':covered_at', self.clock())
            else:
                self.store.put(table+':cursor', last_rowid)
        cursor = self.store.get('event_cursor', 0)
        # Initial load starts near current day using the existing finished index.
        if not cursor and not self.store.get('event_initialized', False):
            rows = self.query('SELECT id FROM pipeline_module_events INDEXED BY pipeline_module_events_time WHERE finished>=? ORDER BY finished,id LIMIT 1', (self.clock()-86400,))
            cursor = max(0, (rows[0]['id'] if rows else 1)-1)
            self.store.put('event_initialized', True)
            self.store.put('events_coverage_start', self.clock()-86400)
        events = self.query('SELECT id,module,owner,sku,started,finished,outcome,details FROM pipeline_module_events WHERE id>? ORDER BY id LIMIT 1000', (cursor,))
        for r in events:
            details = r.pop('details') or ''
            r['module'], r['outcome'] = safe_label(r['module']), safe_label(r['outcome'])
            r['duration_seconds'] = max(0, (r['finished'] or 0)-(r['started'] or 0))
            r['error_class'] = classify(details) if ('error' in r['outcome'].lower() or 'error' in r['module'].lower() or any(w in details.lower() for w in ['error', 'database is locked', 'disk is full'])) else None
            self.store.save('event', str(r['id']), r)
        if events: self.store.put('event_cursor', events[-1]['id'])
        if len(events)<1000: self.store.put('events_covered_at', self.clock())

    def logs(self):
        for name in ('supervisor.log', 'background-tasks.jsonl', 'event-loop-stalls.jsonl'):
            path = self.data/name
            if not path.exists(): continue
            stat = path.stat()
            old = self.store.get('log:'+name, {})
            offset = old.get('offset', max(0, stat.st_size-65536))
            if old.get('inode', stat.st_ino)!=stat.st_ino or offset>stat.st_size: offset=0
            with path.open('rb') as f:
                f.seek(offset); chunk=f.read(65536)
            # Only consume complete lines; partial lines resume on next poll.
            end = chunk.rfind(b'\n')+1
            for i, raw in enumerate(chunk[:end].splitlines()):
                text=raw.decode('utf-8', 'replace')
                if not any(w in text.lower() for w in ('error', 'exception', 'failed', 'restarting', 'stall', 'sqlite_')): continue
                at=None
                try:
                    record=json.loads(text)
                    at=record.get('at') or record.get('time')
                    if not isinstance(at, (int,float)): at=None
                except ValueError: pass
                self.store.save('log', f'{name}:{stat.st_ino}:{offset}:{i}',
                                {'source': name, 'observed_at': self.clock(), 'at': at, 'class': classify(text), 'message': '运行日志异常；详情保留在本机', 'historical_tail': not bool(old)})
            self.store.put('log:'+name, {'inode': stat.st_ino, 'offset': offset+end})

    def progress(self):
        return {'source_tasks': self.query('SELECT id,kind,state,last_at,failures,successes,due FROM sourcing_tasks LIMIT 100'),
                'coverage': {t: self.store.get(t+':covered_at') for t in ('plugin_pipeline','plugin_publications')},
                'events_covered_at': self.store.get('events_covered_at'),
                'failure_observation_started': self.store.get('observation_started')}
