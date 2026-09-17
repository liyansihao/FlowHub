"""Single-attempt ERP relay. Business decisions and journal stay on the Mac.

Commands are never reassigned or replayed, even after a worker disconnects.
Only a locally selected device can receive them; payloads are encrypted at rest.
"""
import json
import secrets
import time
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from fastapi import HTTPException

READS = {'/api.shop/lists', '/api.product.favorite/lists',
         '/api.product.import_logs/index', '/api.product.online/lists',
         '/api.product.online/get_stock'}
WRITES = {'/api.chrome/sku3', '/api.product.favorite/toggle',
          '/api.selection.follow/import', '/api.product.online/batch_update_stock',
          '/api.product.online/sync_shop'}


def validate(request):
    url = urlsplit(request['url'])
    if (url.scheme != 'https' or url.netloc != 'api.maozierp.com'
            or url.fragment or '%' in url.path or '..' in url.path):
        raise ValueError('ERP origin rejected')
    if not ((request['method'] == 'GET' and url.path in READS)
            or (request['method'] == 'POST' and url.path in WRITES)):
        raise ValueError('ERP endpoint rejected')
    if set(k.lower() for k in request['headers']) - {
            'accept', 'accept-language', 'authorization', 'client', 'content-type'}:
        raise ValueError('ERP headers rejected')
    if len(json.dumps(request)) > 2_000_000:
        raise ValueError('ERP payload too large')


class ERPRelay:
    def __init__(self, hub):
        self.hub = hub
        key = hub.directory / 'relay.key'
        try:
            import os
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes())
        with hub.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS erp_devices(device TEXT PRIMARY KEY,version INTEGER,last_seen REAL);
            CREATE TABLE IF NOT EXISTS erp_commands(id TEXT PRIMARY KEY,device TEXT,lease TEXT,
              state TEXT,payload TEXT,result TEXT,created REAL,deadline REAL);
            CREATE TABLE IF NOT EXISTS erp_command_audit(id TEXT PRIMARY KEY,method TEXT,path TEXT,product TEXT);
            ''')

    def encode(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decode(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def submit(self, device, request, product=None):
        validate(request)
        now = self.hub.clock()
        with self.hub.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            ready = c.execute('''SELECT 1 FROM devices d JOIN erp_devices e ON e.device=d.id
                WHERE d.id=? AND d.enabled=1 AND e.version=2 AND e.last_seen>?''',
                (device, now - 10)).fetchone()
            if not ready:
                raise ValueError('Windows production agent is not ready')
            if c.execute("SELECT 1 FROM erp_commands WHERE device=? AND state IN ('queued','claimed','executing') AND deadline>?", (device, now)).fetchone():
                raise ValueError('Windows production agent busy')
            identity = secrets.token_hex(16)
            c.execute('INSERT INTO erp_commands VALUES(?,?,?,?,?,?,?,?)',
                      (identity, device, secrets.token_hex(24), 'queued',
                       self.encode(request), None, now, now + 24))
            c.execute('INSERT INTO erp_command_audit VALUES(?,?,?,?)',
                      (identity,request['method'],urlsplit(request['url']).path,json.dumps(product)))
        return identity

    def claim(self, token):
        now = self.hub.clock()
        with self.hub.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            device = self.hub.auth(c, token)
            c.execute('INSERT OR REPLACE INTO erp_devices VALUES(?,?,?)', (device, 2, now))
            c.execute("UPDATE erp_commands SET state='unknown',payload=NULL WHERE deadline<=? AND state IN ('queued','claimed','executing')", (now,))
            row = c.execute("SELECT * FROM erp_commands WHERE device=? AND state IN ('queued','claimed') AND deadline>? ORDER BY created LIMIT 1", (device, now + 17)).fetchone()
            if not row:
                return None
            c.execute("UPDATE erp_commands SET state='claimed' WHERE id=?", (row['id'],))
            # Request credentials are released only by the one-time begin operation.
            return {'id': row['id'], 'lease': row['lease']}

    def begin(self, token, identity, lease):
        with self.hub.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            device = self.hub.auth(c, token)
            row = c.execute("SELECT * FROM erp_commands WHERE id=? AND device=? AND lease=? AND state='claimed' AND deadline>?", (identity, device, lease, self.hub.clock() + 16)).fetchone()
            if not row:
                raise HTTPException(409, 'Command cannot be executed again')
            c.execute("UPDATE erp_commands SET state='executing',payload=NULL WHERE id=?", (identity,))
            return self.decode(row['payload'])

    def complete(self, token, identity, lease, result):
        if len(json.dumps(result)) > 8_000_000:
            raise HTTPException(413, 'ERP result too large')
        with self.hub.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            device = self.hub.auth(c, token)
            row = c.execute('SELECT * FROM erp_commands WHERE id=? AND device=? AND lease=?', (identity, device, lease)).fetchone()
            if not row:
                raise HTTPException(409, 'Command identity mismatch')
            if row['state'] == 'done':
                if self.decode(row['result']) == result:
                    return {'ok': True}
                raise HTTPException(409, 'Conflicting result')
            if row['state'] != 'executing' or row['deadline'] <= self.hub.clock():
                raise HTTPException(409, 'Command expired; central journal owns recovery')
            c.execute("UPDATE erp_commands SET state='done',result=? WHERE id=?", (self.encode(result), identity))
            return {'ok': True}

    def result(self, identity):
        with self.hub.connect() as c:
            row = c.execute('SELECT * FROM erp_commands WHERE id=?', (identity,)).fetchone()
            if row['state'] == 'done':
                return self.decode(row['result'])
            if row['deadline'] <= self.hub.clock():
                c.execute("UPDATE erp_commands SET state='unknown',payload=NULL WHERE id=?", (identity,))
                return {'error': 'remote_outcome_unknown'}
        return None


def install_routes(app, hub, bearer):
    from pydantic import BaseModel, Field
    from fastapi import Header
    relay = ERPRelay(hub)
    class Command(BaseModel):
        id: str = Field(max_length=64)
        lease: str = Field(max_length=64)
    class Result(Command):
        result: dict
    @app.post('/v2/erp/claim')
    def claim(authorization: str | None = Header(default=None)):
        return {'task': relay.claim(bearer(authorization))}
    @app.post('/v2/erp/begin')
    def begin(body: Command, authorization: str | None = Header(default=None)):
        return relay.begin(bearer(authorization), body.id, body.lease)
    @app.post('/v2/erp/complete')
    def complete(body: Result, authorization: str | None = Header(default=None)):
        return relay.complete(bearer(authorization), body.id, body.lease, body.result)
