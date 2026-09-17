"""Remote read-only compute, separate from the non-replayable ERP write relay."""
import asyncio
import hashlib
import json
import secrets
from fastapi import HTTPException
from .cluster import Coordinator
from .cluster_erp import ERPRelay

KINDS=('rank','screen','dossier')


class Compute:
    def __init__(self,hub):
        self.hub=hub;self.crypto=ERPRelay(hub)
        with hub.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS compute_devices(device TEXT PRIMARY KEY,body TEXT,seen REAL);
            CREATE TABLE IF NOT EXISTS compute_jobs(id TEXT PRIMARY KEY,device TEXT,kind TEXT,
              digest TEXT,payload TEXT,state TEXT,lease TEXT,result TEXT,created REAL,deadline REAL);
            CREATE INDEX IF NOT EXISTS compute_busy ON compute_jobs(device,state,deadline);
            CREATE TABLE IF NOT EXISTS compute_metrics(device TEXT,kind TEXT,samples INTEGER,mean_seconds REAL,
              PRIMARY KEY(device,kind));
            ''')

    def register(self,token,capabilities):
        if capabilities.get('version')!=3 or capabilities.get('slots')!=1:
            raise HTTPException(422,'Unsupported compute capability')
        if not set(capabilities.get('kinds',[]))<=set(KINDS):
            raise HTTPException(422,'Unsupported compute operation')
        with self.hub.connect() as c:
            device=self.hub.auth(c,token)
            c.execute('INSERT OR REPLACE INTO compute_devices VALUES(?,?,?)',
                      (device,json.dumps(capabilities),self.hub.clock()))
        return {'ok':True}

    def submit(self,kind,payload):
        if kind not in KINDS:raise ValueError('Unsupported operation')
        raw=json.dumps(payload,sort_keys=True)
        if len(raw)>2_000_000:raise ValueError('Compute payload too large')
        now=self.hub.clock()
        policy=self.hub.directory/'compute-policy.json'
        if not policy.exists():return None
        config=json.loads(policy.read_text())
        if not config.get('enabled'):return None
        if kind not in config.get('kinds',['rank','screen']):return None
        allowed=config.get('devices',[])
        with self.hub.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE compute_jobs SET state='expired',payload=NULL WHERE deadline<=? AND state IN ('queued','running')",(now,))
            def speed(device):
                measured=c.execute('SELECT samples,mean_seconds FROM compute_metrics WHERE device=? AND kind=?',(device,kind)).fetchone()
                cap=c.execute('SELECT body FROM compute_devices WHERE device=?',(device,)).fetchone()
                info=json.loads(cap[0]) if cap else {}
                # Use observed processing time after three jobs. Initially prefer
                # an available accelerator for image ranking, then more CPU cores.
                baseline=(20 if info.get('accelerator') in ('cuda','mps') else 60) if kind=='rank' else 30
                return (measured['mean_seconds'] if measured and measured['samples']>=3 else baseline,
                        -int(info.get('cpu_count',1)))
            for device in sorted(allowed,key=speed):
                r=c.execute('''SELECT x.body FROM compute_devices x JOIN devices d ON x.device=d.id
                    WHERE d.id=? AND d.enabled=1 AND x.seen>?''',(device,now-20)).fetchone()
                if not r or kind not in json.loads(r['body']).get('kinds',[]):continue
                if c.execute("SELECT 1 FROM compute_jobs WHERE device=? AND state IN ('queued','running') AND deadline>?",(device,now)).fetchone():continue
                identity=secrets.token_hex(16)
                c.execute('INSERT INTO compute_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (identity,device,kind,hashlib.sha256(raw.encode()).hexdigest(),
                           self.crypto.encode(payload),'queued',secrets.token_hex(24),None,now,now+180))
                return identity
        return None

    def claim(self,token):
        with self.hub.connect() as c:
            c.execute('BEGIN IMMEDIATE');device=self.hub.auth(c,token)
            r=c.execute("SELECT * FROM compute_jobs WHERE device=? AND state='queued' AND deadline>? ORDER BY created LIMIT 1",(device,self.hub.clock()+10)).fetchone()
            if not r:return None
            c.execute("UPDATE compute_jobs SET state='running',payload=NULL WHERE id=?",(r['id'],))
            return {'id':r['id'],'lease':r['lease'],'kind':r['kind'],
                    'digest':r['digest'],'payload':self.crypto.decode(r['payload'])}

    def complete(self,token,identity,lease,digest,result):
        if len(json.dumps(result))>2_000_000:raise HTTPException(413,'Result too large')
        with self.hub.connect() as c:
            c.execute('BEGIN IMMEDIATE');device=self.hub.auth(c,token)
            r=c.execute('SELECT * FROM compute_jobs WHERE id=? AND device=? AND lease=? AND digest=?',(identity,device,lease,digest)).fetchone()
            if not r:raise HTTPException(409,'Compute identity mismatch')
            if r['state']=='done':
                if self.crypto.decode(r['result'])==result:return {'ok':True}
                raise HTTPException(409,'Conflicting compute result')
            if r['state']!='running' or r['deadline']<=self.hub.clock():raise HTTPException(409,'Compute result expired')
            c.execute("UPDATE compute_jobs SET state='done',result=? WHERE id=?",(self.crypto.encode(result),identity))
            if not result.get('error'):
                elapsed=max(0,self.hub.clock()-r['created'])
                previous=c.execute('SELECT samples,mean_seconds FROM compute_metrics WHERE device=? AND kind=?',(device,r['kind'])).fetchone()
                samples=previous['samples']+1 if previous else 1
                mean=previous['mean_seconds']*.75+elapsed*.25 if previous else elapsed
                c.execute('INSERT OR REPLACE INTO compute_metrics VALUES(?,?,?,?)',(device,r['kind'],samples,mean))
            return {'ok':True}

    def result(self,identity):
        with self.hub.connect() as c:
            r=c.execute('SELECT state,result,deadline FROM compute_jobs WHERE id=?',(identity,)).fetchone()
            if r['state']=='done':return self.crypto.decode(r['result'])
            if r['deadline']<=self.hub.clock():
                c.execute("UPDATE compute_jobs SET state='expired',payload=NULL WHERE id=?",(identity,))
                return {'error':'compute_deadline'}
        return None


async def remote(kind,payload):
    from .db import DATA
    if not (DATA/'cluster/compute-policy.json').exists():return None
    hub=Compute(Coordinator(DATA/'cluster'))
    identity=hub.submit(kind,payload)
    if identity is None:return None
    while True:
        value=hub.result(identity)
        if value is not None:
            if value.get('error'):
                from .modules import ModuleError
                raise ModuleError('Windows compute failed: '+str(value['error'])[:60])
            return value['output']
        await asyncio.sleep(.25)


def extra_review_workers(directory):
    policy=directory/'cluster/compute-policy.json'
    if not policy.exists():return 0
    config=json.loads(policy.read_text())
    if not config.get('enabled'):return 0
    hub=Compute(Coordinator(directory/'cluster'))
    with hub.hub.connect() as c:
        live={r[0] for r in c.execute('''SELECT x.device,x.body FROM compute_devices x JOIN devices d ON x.device=d.id
            WHERE d.enabled=1 AND x.seen>?''',(hub.hub.clock()-20,)) if 'rank' in json.loads(r[1]).get('kinds',[])}
    return min(2,int(config.get('extra_review_workers',1)),len(live&set(config.get('devices',[]))))


def install_routes(app,hub,bearer):
    from fastapi import Header
    from pydantic import BaseModel,Field
    service=Compute(hub)
    class Capability(BaseModel):
        version:int=3
        slots:int=1
        kinds:list[str]=Field(max_length=3)
        cpu_count:int=Field(ge=1,le=1024)
        accelerator:str=Field(max_length=100)
    class Result(BaseModel):
        id:str=Field(max_length=64)
        lease:str=Field(max_length=64)
        digest:str=Field(max_length=64)
        result:dict
    @app.post('/v3/compute/register')
    def register(body:Capability,authorization:str|None=Header(default=None)):
        return service.register(bearer(authorization),body.model_dump())
    @app.post('/v3/compute/claim')
    def claim(authorization:str|None=Header(default=None)):
        return {'task':service.claim(bearer(authorization))}
    @app.post('/v3/compute/complete')
    def complete(body:Result,authorization:str|None=Header(default=None)):
        return service.complete(bearer(authorization),body.id,body.lease,body.digest,body.result)
