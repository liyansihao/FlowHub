"""Module pause switches and per-operation timing, without changing business gates."""
import json
import time

MODULES = ('seed', 'review', 'publication')


def schema(db):
    def initialize():
        with db.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS pipeline_module_control(module TEXT PRIMARY KEY,paused INTEGER NOT NULL,updated REAL NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS pipeline_module_events(id INTEGER PRIMARY KEY,module TEXT,owner TEXT,sku TEXT,started REAL,finished REAL,outcome TEXT,details TEXT)')
            c.execute('CREATE INDEX IF NOT EXISTS pipeline_module_events_time ON pipeline_module_events(finished)')
    db.schema_once('pipeline_modules.control', initialize)


def paused(db, module):
    with db.connect() as c:
        row = c.execute('SELECT paused FROM pipeline_module_control WHERE module=?', (module,)).fetchone()
    return bool(row and row[0])


def set_paused(db, module, value):
    if module not in MODULES:
        raise ValueError('unknown module')
    schema(db)
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO pipeline_module_control VALUES(?,?,?)', (module, int(value), time.time()))


def record(db, module, owner, sku, started, outcome, details=None):
    with db.connect() as c:
        c.execute('INSERT INTO pipeline_module_events(module,owner,sku,started,finished,outcome,details) VALUES(?,?,?,?,?,?,?)',
                  (module, owner, sku, started, time.time(), outcome, json.dumps(details or {})))
