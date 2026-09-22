"""Persistent source expansion and fair, bounded Playwright store collection.

Configuration is opt-in. Publication authorization remains in admission.py.
"""
import asyncio
import fcntl
import json
import os
import time
from pathlib import Path
from ..browser_source import BrowserSource
from ..source_acquisition import erp_request
from . import control
from .database_work import run as database_work

SEED_RESOLUTION_MAX_ATTEMPTS = 4  # Initial read plus three delayed retries.


def schema(db):
    def initialize():
        from .admission import schema as admission_schema
        admission_schema(db)
        BrowserSource(db)
        control.schema(db)
        with db.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS source_loop_stores(
              owner TEXT,seller TEXT,run_id TEXT,due REAL,failures INTEGER DEFAULT 0,
              last_state TEXT,updated REAL,PRIMARY KEY(owner,seller));
            CREATE TABLE IF NOT EXISTS source_seed_resolutions(
              owner TEXT,sku TEXT,state TEXT,seller TEXT,reason TEXT,due REAL,attempts INTEGER,
              evidence TEXT,updated REAL,PRIMARY KEY(owner,sku));
            ''')
    db.schema_once('pipeline_modules.source_loop', initialize)


def sync_stores(db, owner, run_id, now=None):
    """Only evidenced roots enter discovery; collected products are not invented roots."""
    now=time.time() if now is None else now
    service=BrowserSource(db);manifest={}
    with db.connect() as c:
        # Reuse the original exact SKU/offer bindings, not an inferred offer ID.
        for scan in c.execute('SELECT seller,roots FROM browser_source_scans WHERE owner=? AND run_id=?',(owner,run_id)).fetchall():
            for root in json.loads(scan['roots']):
                sku=str(root.get('source_sku') or root.get('sku') or '')
                shop=str(root.get('shop') or '');offer=root.get('offer')
                r=c.execute('SELECT body FROM sourcing_seeds WHERE owner=? AND sku=? AND shop=? AND offer=?',(owner,sku,shop,offer)).fetchone()
                if r:
                    body=json.loads(r[0])
                    if not body.get('seller_id'):
                        body.update(seller_id=scan['seller'],seller_resolution={'channel':'historical-exact-store-root','run_id':run_id,'sku':sku})
                        c.execute('UPDATE sourcing_seeds SET body=? WHERE owner=? AND shop=? AND offer=?',(json.dumps(body),owner,shop,offer))
        # Completed publications give exact own offer -> original SKU/seller bindings.
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone():
            for r in c.execute("SELECT json_object('sku',json_extract(body,'$.sku'),'seller',json_extract(body,'$.seller'),'offer_id',json_extract(body,'$.offer_id'),'product',json_extract(body,'$.product')) AS body FROM plugin_publications WHERE owner=? AND json_extract(body,'$.verified')=1",(owner,)).fetchall():
                p=json.loads(r[0]);product=p.get('product') or {}
                shop=str(product.get('shop_id') or '');sku=str(p.get('sku') or '');seller=str(p.get('seller') or '')
                offer=p.get('offer_id')
                if not (shop.isdigit() and sku.isdigit() and seller.isdigit() and offer):continue
                body={'sku':sku,'seller_id':seller,'evidence':{'channel':'verified-publication','offer_id':offer},'online_evidence':product}
                c.execute('INSERT OR IGNORE INTO sourcing_seeds VALUES(?,?,?,?,?,0,NULL,?)',(owner,shop,offer,sku,json.dumps(body),now))
        for r in c.execute("""SELECT sku,shop,offer,body FROM sourcing_seeds s WHERE owner=? AND archived=0
          AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.sku)""",(owner,)):
            body=json.loads(r['body']);seller=str(body.get('seller_id') or '')
            if seller.isdigit() and int(seller)>0:
                manifest.setdefault(seller,[]).append({'sku':r['sku'],'shop':r['shop'],'offer':r['offer'],'channel':'active-seed'})
        # Historical scans retain their provenance; they are never reset by discovery.
        existing=[dict(r) for r in c.execute('SELECT * FROM browser_source_scans WHERE owner=? AND run_id=?',(owner,run_id))]
    service.prepare(owner,run_id,manifest)
    enrolled=0
    with db.connect() as c:
        for seller in set(manifest)|{r['seller'] for r in existing}:
            inserted=c.execute('INSERT OR IGNORE INTO source_loop_stores VALUES(?,?,?,0,0,?,?)',(owner,seller,run_id,'enrolled',now)).rowcount
            if inserted:
                # Enrolment is authorized by the opt-in loop configuration. Later
                # manual pauses remain paused; sync never resumes existing tasks.
                c.execute("UPDATE browser_source_scans SET state='ready' WHERE owner=? AND run_id=? AND seller=? AND state='paused'",(owner,run_id,seller))
                enrolled+=1
    return enrolled


async def resolve_one(db,owner,request=erp_request,now=None):
    now=time.time() if now is None else now
    with db.connect() as c:
        seed=c.execute("""SELECT s.*,COALESCE(r.attempts,0) AS resolution_attempts FROM sourcing_seeds s LEFT JOIN source_seed_resolutions r
          ON r.owner=s.owner AND r.sku=s.sku WHERE s.owner=? AND s.archived=0
          AND (json_extract(s.body,'$.seller_id') IS NULL OR json_extract(s.body,'$.seller_id')='')
          AND COALESCE(r.due,0)<=? AND COALESCE(r.state,'')!='retry_exhausted'
          AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.sku)
          ORDER BY COALESCE(s.sales,0) DESC,s.sku LIMIT 1""",(owner,now)).fetchone()
        setting=c.execute('SELECT secret FROM sourcing_settings WHERE owner=? AND enabled=1',(owner,)).fetchone()
    if not seed or not setting:return {'state':'no_due_seed'}
    sku=seed['sku'];seller=None;reason=None;evidence={}
    if seed['resolution_attempts']>=SEED_RESOLUTION_MAX_ATTEMPTS:
        # Retain the old evidence/attempt count. An exhausted seed must not
        # consume another remote request or prevent the next seed from running.
        with db.connect() as c:
            c.execute("UPDATE source_seed_resolutions SET state='retry_exhausted',updated=? WHERE owner=? AND sku=?",
                      (now,owner,sku))
        return {'state':'retry_exhausted','sku':sku,'seller':None,'reason':'seed_resolution_retry_budget_exhausted'}
    try:
        # Reuse exact page observations already in this owner's library first.
        # Multiple sellers for a SKU require a current direct resolution.
        with db.connect() as c:
            known=c.execute("""SELECT DISTINCT seller FROM sourcing_products WHERE owner=? AND sku=?
              AND json_extract(body,'$.coverage') IN ('storefront-page','maozi-exact-seller-page')""",(owner,sku)).fetchall()
        if len(known)==1:
            data={'sku':sku,'seller_id':known[0][0]};channel='exact-source-library'
        else:
            result=await request('/api.chrome/sku3',{'sku':sku},db.open(setting[0])['erp_token'])
            data=result.get('data',result) if isinstance(result,dict) else {};channel='maozi-sku3'
        evidence={'channel':channel,'requested_sku':sku,'observed_at':now,
                  'returned_sku':data.get('sku') if isinstance(data,dict) else None,
                  'returned_fields':sorted(data) if isinstance(data,dict) else []}
        if not isinstance(data,dict) or str(data.get('sku'))!=sku:raise ValueError('seed_identity_mismatch')
        seller=str(data.get('sellerId') or data.get('seller_id') or '')
        if not seller.isdigit() or int(seller)<=0:raise ValueError('seller_missing_in_direct_response')
        evidence.update(sku=sku,seller_id=seller,resolved_at=now)
    except Exception as error:
        from ..source_acquisition import AcquisitionError
        seller=None;reason=str(error)[:120] if isinstance(error,(ValueError,AcquisitionError)) else type(error).__name__
    state='resolved' if seller else ('retry_exhausted' if seed['resolution_attempts']+1>=SEED_RESOLUTION_MAX_ATTEMPTS else 'waiting')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('''INSERT INTO source_seed_resolutions VALUES(?,?,?,?,?,?,1,?,?)
         ON CONFLICT(owner,sku) DO UPDATE SET state=excluded.state,seller=excluded.seller,
         reason=excluded.reason,due=excluded.due,attempts=attempts+1,evidence=excluded.evidence,updated=excluded.updated''',
         (owner,sku,state,seller,reason,now+(86400 if seller else 21600),json.dumps(evidence),now))
        if seller:
            for r in c.execute('SELECT shop,offer,body FROM sourcing_seeds WHERE owner=? AND sku=?',(owner,sku)).fetchall():
                body=json.loads(r['body']);body.update(seller_id=seller,seller_resolution=evidence)
                c.execute('UPDATE sourcing_seeds SET body=? WHERE owner=? AND shop=? AND offer=?',(json.dumps(body),owner,r['shop'],r['offer']))
    return {'state':state,'sku':sku,'seller':seller,'reason':reason}


def choose(db,owner,now=None):
    now=time.time() if now is None else now
    with db.connect() as c:
        return c.execute('''SELECT m.*,s.state,s.page,s.roots FROM source_loop_stores m
          JOIN browser_source_scans s ON s.owner=m.owner AND s.seller=m.seller AND s.run_id=m.run_id
          WHERE m.owner=? AND m.due<=? AND s.state IN ('ready','done','blocked')
          AND (s.state='ready' OR m.last_state NOT IN ('blocked','retry_exhausted'))
          ORDER BY m.updated,m.seller LIMIT 1''',(owner,now)).fetchone()


def finish(db,task,now=None):
    now=time.time() if now is None else now
    service=BrowserSource(db);key=(task['owner'],task['run_id'],task['seller'])
    state=service.next_request(*key)['state'];reason=None;failures=task['failures']
    with db.connect() as c:
        if state=='blocked':
            r=c.execute('SELECT reason FROM browser_source_failures WHERE owner=? AND run_id=? AND seller=? ORDER BY at DESC LIMIT 1',key).fetchone()
            reason=r[0] if r else 'unknown';failures+=1
        else:failures=0
        retry=bool(reason and (reason.startswith('browser_navigation_') or reason in ('browser_page_state_unavailable','browser_http_429','browser_http_500','browser_http_502','browser_http_503','browser_http_504')))
        outcome=('retry_wait' if failures<=3 else 'retry_exhausted') if retry else state
        delay=min(3600,60*2**failures) if retry else 86400 if state=='done' else 10
        c.execute('UPDATE source_loop_stores SET due=?,failures=?,last_state=?,updated=? WHERE owner=? AND seller=?',
                  (now+delay,failures,outcome,now,task['owner'],task['seller']))
    return {'state':outcome,'seller':task['seller'],'reason':reason}


async def collect(db,task,config):
    service=BrowserSource(db);key=(task['owner'],task['run_id'],task['seller'])
    if task['state']=='blocked':service.control(*key,'retry')
    if task['state']=='done':
        # New generation retains prior receipts, failures and end-of-store proof.
        run_id='source-refresh-'+str(time.time_ns())
        service.prepare(task['owner'],run_id,{task['seller']:json.loads(task['roots'])})
        service.control(task['owner'],run_id,task['seller'],'resume')
        with db.connect() as c:c.execute('UPDATE source_loop_stores SET run_id=? WHERE owner=? AND seller=?',(run_id,task['owner'],task['seller']))
        task=dict(task)|{'run_id':run_id};key=(task['owner'],run_id,task['seller'])
    root=Path(__file__).resolve().parents[2]
    browser_env={'FLOWHUB_SOURCE_PROFILE':config['profile'],'FLOWHUB_DATA':str(db.directory.resolve())}
    if config.get('extension_dir'):browser_env['FLOWHUB_SOURCE_EXTENSION_DIR']=str(config['extension_dir'])
    if config.get('chromium_executable'):browser_env['FLOWHUB_SOURCE_CHROMIUM_EXECUTABLE']=str(config['chromium_executable'])
    process=await asyncio.create_subprocess_exec('node',str(root/'bridges/playwright-source.mjs'),*key,str(config.get('pages_per_store',3)),
        cwd=root,env=os.environ|browser_env,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        await asyncio.wait_for(process.communicate(),240)
        if process.returncode and service.next_request(*key)['state']=='ready':service.fail(*key,'browser_worker_exit')
    except asyncio.TimeoutError:
        service.fail(*key,'browser_navigation_TimeoutError')
    finally:
        if process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),15)
            except asyncio.TimeoutError:process.kill();await process.wait()
    return await database_work(finish,db,task)


def backlog_state(db,owner):
    with db.connect() as c:
        last=c.execute('SELECT MAX(updated) FROM source_seed_resolutions WHERE owner=?',(owner,)).fetchone()[0] or 0
        backlog=c.execute('''SELECT COUNT(DISTINCT p.sku) FROM sourcing_products p WHERE p.owner=?
          AND json_extract(p.body,'$.coverage') IN ('storefront-page','maozi-exact-seller-page')
          AND NOT EXISTS(SELECT 1 FROM pipeline_admissions a WHERE a.owner=p.owner AND a.sku=p.sku)
          AND NOT EXISTS(SELECT 1 FROM plugin_pipeline q WHERE q.owner=p.owner AND q.sku=p.sku)
          AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.owner=p.owner AND j.source_key=p.sku)
          AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=p.owner AND b.source_key=p.sku)''',(owner,)).fetchone()[0]
    return last,backlog


async def tick(db,config):
    await database_work(schema,db)
    if not config.get('enabled') or await database_work(control.paused,db,'seed'):return {'state':'paused'}
    owner=config['owner'];now=time.time()
    # One worker per database/profile. OS releases the lock after a crash.
    with (db.directory/'source-loop.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'busy'}
        enrolled=await database_work(sync_stores,db,owner,config['run_id'])
        last,backlog=await database_work(backlog_state,db,owner)
        if now-last>=config.get('resolve_interval_seconds',120):
            result=await resolve_one(db,owner)
            await database_work(control.record,db,'seed',owner,result.get('sku',''),now,result['state'],result)
            await database_work(sync_stores,db,owner,config['run_id'])
        if await database_work(control.paused,db,'seed'):return {'state':'paused'}
        if config.get('other_sellers_enabled'):
            from .. import other_sellers
            await database_work(other_sellers.schema,db)
            await database_work(other_sellers.prepare_samples,db,owner)
            await database_work(other_sellers.promote_qualified,db,owner)
            with db.connect() as c:
                due=c.execute('SELECT due FROM source_discovery_clock WHERE owner=?',(owner,)).fetchone()
                discovery_due=not due or now>=due[0]
                if discovery_due:
                    c.execute('INSERT OR REPLACE INTO source_discovery_clock VALUES(?,?)',
                        (owner,now+config.get('discovery_interval_seconds',120)))
            if discovery_due:
                await database_work(other_sellers.replenish,db,owner,explore_pending=config.get('explore_pending_sources',False))
                result=await other_sellers.discover_one(db,owner,config)
                await database_work(control.record,db,'seed',owner,result.get('sku',''),now,'other_sellers',result)
                await database_work(other_sellers.prepare_samples,db,owner)
            if discovery_due and backlog<config.get('max_source_backlog',2000):
                result=await other_sellers.sample_one(db,owner,config)
                if result['state']!='no_due_sample':return result
        if backlog>=config.get('max_source_backlog',2000):return {'state':'source_backpressure','backlog':backlog}
        task=await database_work(choose,db,owner)
        result=await collect(db,task,config) if task else {'state':'no_due_store'}
        return result|{'enrolled':enrolled,'backlog':backlog}


async def run(db):
    config_path=db.directory/'source-loop.json'
    while True:
        started=time.time();config={}
        try:
            if config_path.exists():
                config=json.loads(config_path.read_text())
                result=await tick(db,config)
                await database_work(control.record,db,'seed',config.get('owner',''),'',started,result['state'],result)
        except Exception as error:
            await database_work(control.schema,db)
            await database_work(control.record,db,'seed',config.get('owner',''),'',started,'source_loop_error',{'reason':type(error).__name__})
        await asyncio.sleep(5)


def status(db,owner):
    """Public operational summary; no profile, credentials or browser page tokens."""
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='source_loop_stores'").fetchone():return {'configured':False}
        states={r[0]:r[1] for r in c.execute('SELECT last_state,COUNT(*) FROM source_loop_stores WHERE owner=? GROUP BY last_state',(owner,))}
        resolutions={r[0]:r[1] for r in c.execute('SELECT state,COUNT(*) FROM source_seed_resolutions WHERE owner=? GROUP BY state',(owner,))}
        candidates=c.execute('''SELECT DISTINCT e.sku,json_extract(e.body,'$.provenance.seller_id') seller,q.state
          FROM sourcing_evidence e LEFT JOIN plugin_pipeline q ON q.owner=e.owner AND q.sku=e.sku
          AND q.seller=json_extract(e.body,'$.provenance.seller_id')
          WHERE e.owner=? AND json_extract(e.body,'$.provenance.channel')='browser-page-source'
          AND json_extract(e.body,'$.provenance.browser')='playwright' ''',(owner,)).fetchall()
        discovery={}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='source_discovery_seeds'").fetchone():
            discovery['seed_states']={r[0]:r[1] for r in c.execute('SELECT state,COUNT(*) FROM source_discovery_seeds WHERE owner=? GROUP BY state',(owner,))}
            discovery['observed_other_sellers']=c.execute('SELECT COUNT(DISTINCT seller) FROM source_other_sellers WHERE owner=?',(owner,)).fetchone()[0]
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='source_discovered_stores'").fetchone():
            discovery['store_assessments']={r[0]:r[1] for r in c.execute('SELECT state,COUNT(*) FROM source_discovered_stores WHERE owner=? GROUP BY state',(owner,))}
    from collections import Counter
    return {'configured':True,'stores':sum(states.values()),'store_states':states,'seed_resolutions':resolutions,
            'browser_new_candidates':len(candidates),'candidate_states':dict(Counter(r['state'] or 'not_admitted' for r in candidates)),
            'other_seller_discovery':discovery}
