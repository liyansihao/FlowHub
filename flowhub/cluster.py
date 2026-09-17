"""Central device registry and fenced, durable read-only acceptance jobs.

Production writes are deliberately not dispatched by this first-stage protocol.
"""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


def digest(value):return hashlib.sha256(value.encode()).hexdigest()


class Coordinator:
    def mode(self):
        policy=self.directory/'production-routing.json'
        if policy.exists():
            config=json.loads(policy.read_text())
            if config.get('enabled'):
                return 'windows_production' if config.get('mode')=='continuous' else 'windows_canary'
        return 'acceptance_only'

    def __init__(self,directory,clock=time.time):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.directory/'cluster.sqlite3';self.clock=clock
        with self.connect() as c:
            # Device heartbeats/results must not wait behind status readers.
            # WAL keeps reads concurrent with the single durable writer.
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            c.executescript('''
            CREATE TABLE IF NOT EXISTS enrollments(hash TEXT PRIMARY KEY,name TEXT,expires REAL,used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT,token_hash TEXT UNIQUE,enabled INTEGER,last_seen REAL,platform TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,dedupe TEXT UNIQUE,kind TEXT,payload TEXT,state TEXT,device TEXT,lease TEXT,expires REAL,attempt INTEGER DEFAULT 0,result TEXT,result_hash TEXT,created REAL);
            CREATE INDEX IF NOT EXISTS task_queue ON tasks(state,expires,created);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()

    def invitation(self,name):
        code=secrets.token_urlsafe(32)
        with self.connect() as c:c.execute('INSERT INTO enrollments(hash,name,expires) VALUES(?,?,?)',(digest(code),name,self.clock()+1800))
        return code

    def enroll(self,code,platform):
        now=self.clock()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            invite=c.execute('SELECT * FROM enrollments WHERE hash=? AND used=0 AND expires>?',(digest(code),now)).fetchone()
            if not invite:raise HTTPException(401,'Enrollment expired or already used')
            device=secrets.token_hex(16);token=secrets.token_urlsafe(48)
            c.execute('UPDATE enrollments SET used=1 WHERE hash=?',(digest(code),))
            c.execute('INSERT INTO devices VALUES(?,?,?,?,?,?)',(device,invite['name'],digest(token),1,now,platform))
            return {'device_id':device,'token':token,'mode':'acceptance_only'}

    def auth(self,c,token):
        row=c.execute('SELECT * FROM devices WHERE token_hash=? AND enabled=1',(digest(token),)).fetchone()
        if not row:raise HTTPException(401,'Device not authorized')
        c.execute('UPDATE devices SET last_seen=? WHERE id=?',(self.clock(),row['id']))
        return row['id']

    def enqueue(self,dedupe):
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO tasks(id,dedupe,kind,payload,state,created) VALUES(?,?,?,?,?,?)',
                      (secrets.token_hex(16),dedupe,'probe',json.dumps({'challenge':secrets.token_hex(16)}),'queued',self.clock()))
            return c.execute('SELECT id FROM tasks WHERE dedupe=?',(dedupe,)).fetchone()[0]

    def claim(self,token):
        now=self.clock()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');device=self.auth(c,token)
            # A lost claim response returns the SAME lease to that device.
            row=c.execute("SELECT * FROM tasks WHERE device=? AND state='leased' AND expires>?",(device,now)).fetchone()
            if not row:
                row=c.execute("SELECT * FROM tasks WHERE state='queued' OR (state='leased' AND expires<=?) ORDER BY created LIMIT 1",(now,)).fetchone()
                if not row:return None
                c.execute("UPDATE tasks SET state='leased',device=?,lease=?,expires=?,attempt=attempt+1 WHERE id=?",
                          (device,secrets.token_hex(24),now+120,row['id']))
                row=c.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone()
            return {'id':row['id'],'kind':row['kind'],'payload':json.loads(row['payload']),'lease':row['lease'],'expires':row['expires']}

    def heartbeat(self,token,task_id=None,lease=None):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');device=self.auth(c,token)
            if task_id:
                changed=c.execute("UPDATE tasks SET expires=? WHERE id=? AND device=? AND lease=? AND state='leased' AND expires>?",
                                  (self.clock()+120,task_id,device,lease,self.clock())).rowcount
                if not changed:raise HTTPException(409,'Lease no longer current')
            return {'ok':True,'mode':'acceptance_only'}

    def complete(self,token,task_id,lease,result):
        encoded=json.dumps(result,sort_keys=True,separators=(',',':'));hashed=digest(encoded)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');device=self.auth(c,token)
            r=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not r or r['device']!=device or r['lease']!=lease:raise HTTPException(409,'Lease no longer current')
            if r['state']=='done':
                if r['result_hash']==hashed:return {'ok':True,'duplicate':True}
                raise HTTPException(409,'Result differs from accepted result')
            if r['state']!='leased' or r['expires']<=self.clock():raise HTTPException(409,'Lease expired')
            if r['kind']!='probe' or result.get('challenge')!=json.loads(r['payload'])['challenge']:
                raise HTTPException(422,'Acceptance result invalid')
            c.execute("UPDATE tasks SET state='done',result=?,result_hash=? WHERE id=?",(encoded,hashed,task_id))
            return {'ok':True,'duplicate':False}

    def status(self):
        with self.connect() as c:
            return {'mode':self.mode(),'devices':[dict(r) for r in c.execute('SELECT id,name,enabled,last_seen,platform FROM devices')],
                    'tasks':dict(c.execute('SELECT state,count(*) FROM tasks GROUP BY state').fetchall())}

    def revoke(self,device):
        with self.connect() as c:
            if not c.execute('UPDATE devices SET enabled=0 WHERE id=?',(device,)).rowcount:raise ValueError('Unknown device')


class Enrollment(BaseModel):
    code:str=Field(min_length=20,max_length=200)
    platform:str=Field(max_length=100)
class Beat(BaseModel):
    task_id:str|None=Field(default=None,max_length=64)
    lease:str|None=Field(default=None,max_length=64)
class Completion(BaseModel):
    task_id:str=Field(max_length=64)
    lease:str=Field(max_length=64)
    challenge:str=Field(max_length=64)
    platform:str=Field(max_length=100)


def create_app(directory=None):
    from .db import DATA
    hub=Coordinator(directory or DATA/'cluster');app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None);app.state.hub=hub
    def bearer(authorization):
        if not authorization or not authorization.startswith('Bearer '):raise HTTPException(401,'Device token required')
        return authorization[7:]
    @app.get('/healthz')
    def health():return {'ok':True,'mode':hub.mode(),'protocol':1,'erp_protocol':2,'compute_protocol':3}
    @app.post('/v1/enroll')
    def enroll(body:Enrollment):return hub.enroll(body.code,body.platform)
    @app.post('/v1/claim')
    def claim(authorization:str|None=Header(default=None)):return {'task':hub.claim(bearer(authorization))}
    @app.post('/v1/heartbeat')
    def heartbeat(body:Beat,authorization:str|None=Header(default=None)):
        return hub.heartbeat(bearer(authorization),body.task_id,body.lease)
    @app.post('/v1/complete')
    def complete(body:Completion,authorization:str|None=Header(default=None)):
        return hub.complete(bearer(authorization),body.task_id,body.lease,{'challenge':body.challenge,'platform':body.platform})
    from .cluster_erp import install_routes
    install_routes(app, hub, bearer)
    from .cluster_compute import install_routes as install_compute
    install_compute(app,hub,bearer)
    return app
