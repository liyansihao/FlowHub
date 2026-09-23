import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entities(kind TEXT, key TEXT, body TEXT, dirty INTEGER DEFAULT 1,
                    PRIMARY KEY(kind,key));
                CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, body TEXT, result TEXT);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=2)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def get(self, key, default=None):
        with self.connect() as c:
            row = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, json.dumps(value)))

    def save(self, kind, key, body):
        previous = self.entity(kind, key)
        if previous and {k:v for k,v in previous.items() if k!='observed_at'} == {k:v for k,v in body.items() if k!='observed_at'}:
            return
        value = json.dumps(body, sort_keys=True)
        with self.connect() as c:
            c.execute('''INSERT INTO entities VALUES(?,?,?,1) ON CONFLICT(kind,key)
                         DO UPDATE SET body=excluded.body,dirty=1 WHERE body<>excluded.body''', (kind, key, value))

    def items(self, kind):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT body FROM entities WHERE kind=?', (kind,))]

    def batch(self):
        with self.connect() as c:
            rows=[]
            for kind,limit in [('event',500),('log',50),('product',150),('publication',150),('success',100),('failure',50)]:
                rows.extend(dict(r) for r in c.execute('SELECT kind,key,body FROM entities WHERE dirty=1 AND kind=? ORDER BY key LIMIT ?', (kind,limit)))
            for kind in ('product','publication','success','failure','log','event'):
                remaining = 1000-len(rows)
                if remaining<=0: break
                offset = sum(r['kind']==kind for r in rows)
                rows.extend(dict(r) for r in c.execute('SELECT kind,key,body FROM entities WHERE dirty=1 AND kind=? ORDER BY key LIMIT ? OFFSET ?', (kind,remaining,offset)))
            return rows

    def pending_counts(self):
        with self.connect() as c:
            return {r[0]:r[1] for r in c.execute("SELECT kind,count(*) FROM entities WHERE dirty=1 GROUP BY kind")}

    def entity(self, kind, key):
        with self.connect() as c:
            r=c.execute("SELECT body FROM entities WHERE kind=? AND key=?",(kind,key)).fetchone()
            return json.loads(r[0]) if r else None

    def ack(self, batch):
        # Only clear the exact uploaded revision; a newer local value stays dirty.
        with self.connect() as c:
            c.executemany('UPDATE entities SET dirty=0 WHERE kind=? AND key=? AND body=?',
                          [(r['kind'], r['key'], r['body']) for r in batch])

    def begin_command(self, cmd):
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO commands VALUES(?,?,NULL)', (cmd['id'], json.dumps(cmd, sort_keys=True)))
            row = c.execute('SELECT * FROM commands WHERE id=?', (cmd['id'],)).fetchone()
        if row['body'] != json.dumps(cmd, sort_keys=True):
            raise ValueError('command_identity_conflict')
        return json.loads(row['result']) if row['result'] else None

    def result(self, identity, result):
        with self.connect() as c:
            c.execute('UPDATE commands SET result=? WHERE id=?', (json.dumps(result), identity))
