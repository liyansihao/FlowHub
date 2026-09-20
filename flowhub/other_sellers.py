"""SKU -> observed other sellers. Separate from owned-offer/order seeds."""
import hashlib
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin,urlsplit,parse_qs
from .source_library import SourceLibrary,SourceFilters,assess,identity

class OffersHTML(HTMLParser):
    def __init__(self):
        super().__init__();self.depth=0;self.scope=None;self.links=[];self.offer=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag not in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):
            self.depth+=1
        if a.get('data-widget')=='webSellerList':self.scope=self.depth
        if a.get('id','').startswith('state-webBestSeller-'):
            self.offer=json.loads(a.get('data-state','{}'))
        if self.scope is not None and tag=='a' and '/seller/' in a.get('href',''):
            self.links.append(a['href'])
    def handle_endtag(self,tag):
        if self.scope==self.depth:self.scope=None
        self.depth=max(0,self.depth-1)

def parse_offers(html,sku):
    sku=identity(sku);p=OffersHTML();p.feed(html)
    if not p.offer:raise ValueError('other_offers_entry_not_observed')
    link=p.offer.get('modalLink','').replace(r'\u002F','/')
    url=urlsplit(urljoin('https://www.ozon.ru',link))
    if url.netloc!='www.ozon.ru' or url.path!='/modal/otherOffersFromSellers' or parse_qs(url.query).get('product_id')!=[sku]:
        raise ValueError('other_offers_seed_mismatch')
    sellers=set()
    for link in p.links:
        u=urlsplit(urljoin('https://www.ozon.ru',link))
        m=re.fullmatch(r'/seller/(?:[^/]+-)?(\d+)/?',u.path)
        if u.scheme=='https' and u.netloc=='www.ozon.ru' and m:sellers.add(identity(m[1]))
    try:expected=int(p.offer.get('count'))
    except (ValueError,TypeError):expected=None
    # Exact advertised count is conservative: duplicate offers, absent count,
    # or unloaded pages remain partial. Never infer end from a quiet viewport.
    complete=bool(expected is not None and expected>0 and len(sellers)==expected)
    return {'sku':sku,'sellers':sorted(sellers),'expected':expected,'complete':complete,
            'reason':None if complete else 'partial_other_sellers','modal_url':url.geturl()}

def schema(db):
    SourceLibrary(db)
    with db.connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS source_discovery_seeds(owner TEXT,sku TEXT,state TEXT,due REAL,
          attempts INTEGER,body TEXT,updated REAL,PRIMARY KEY(owner,sku));
        CREATE TABLE IF NOT EXISTS source_other_sellers(owner TEXT,seed_sku TEXT,seller TEXT,body TEXT,
          observed REAL,PRIMARY KEY(owner,seed_sku,seller));
        CREATE TABLE IF NOT EXISTS source_discovery_receipts(owner TEXT,sku TEXT,digest TEXT,body TEXT,
          observed REAL,PRIMARY KEY(owner,sku,digest));
        CREATE TABLE IF NOT EXISTS source_discovery_clock(owner TEXT PRIMARY KEY,due REAL);
        CREATE TABLE IF NOT EXISTS source_discovery_checks(owner TEXT,sku TEXT,due REAL,body TEXT,
          PRIMARY KEY(owner,sku));
        CREATE TABLE IF NOT EXISTS source_discovery_failures(owner TEXT,sku TEXT,reason TEXT,at REAL);
        ''')

def replenish(db,owner,limit=20,now=None,explore_pending=False):
    """Only source-qualified goods; no publication/order requirement or invented offer."""
    now=time.time() if now is None else now;added=0
    with db.connect() as c:
        if c.execute("SELECT COUNT(*) FROM source_discovery_seeds WHERE owner=? AND state!='complete'",(owner,)).fetchone()[0]>=500:return 0
        setting=c.execute('SELECT body FROM sourcing_settings WHERE owner=?',(owner,)).fetchone()
        filters=SourceFilters(**json.loads(setting[0])) if setting else SourceFilters()
        rows=c.execute('''SELECT p.* FROM sourcing_products p WHERE owner=?
          AND json_extract(body,'$.coverage') IN ('storefront-page','maozi-exact-seller-page')
          AND NOT EXISTS(SELECT 1 FROM source_discovery_seeds s WHERE s.owner=p.owner AND s.sku=p.sku)
          AND NOT EXISTS(SELECT 1 FROM source_discovery_checks s WHERE s.owner=p.owner AND s.sku=p.sku AND s.due>?)
          AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=p.owner AND b.source_key=p.sku)
          ORDER BY p.id DESC LIMIT 500''',(owner,now)).fetchall()
        for r in rows:
            p=json.loads(r['body']);result=assess(p,filters,now)
            c.execute('INSERT OR REPLACE INTO source_discovery_checks VALUES(?,?,?,?)',(owner,r['sku'],now+3600,json.dumps(result)))
            roots=(p.get('source_relation') or {}).get('root_seeds') or []
            exploration=bool(explore_pending and result['state']=='needs_review' and not result['failed']
                and p.get('title') and p.get('image') and p.get('evidence_hash')
                and any(x.get('channel')=='other-seller-discovery' for x in roots))
            if result['state']!='qualified' and not exploration:continue
            parent_generations=[]
            for root in roots:
                parent=c.execute('SELECT body FROM source_discovery_seeds WHERE owner=? AND sku=?',
                    (owner,str(root.get('source_sku') or root.get('sku') or ''))).fetchone()
                if parent:parent_generations.append(json.loads(parent[0]).get('generation',1))
            body={'origin_seller':r['seller'],'source_evidence_hash':p.get('evidence_hash'),'assessment':result,
                  'exploration_only':exploration,'parent_root_seeds':roots,
                  'generation':max(parent_generations,default=0)+1}
            added+=c.execute('INSERT OR IGNORE INTO source_discovery_seeds VALUES(?,?,?,0,0,?,?)',
                (owner,r['sku'],'queued',json.dumps(body),now)).rowcount
            if added>=limit:break
    return added

def ingest(db,owner,sku,html,artifact,now=None):
    now=time.time() if now is None else now
    packet=parse_offers(html,sku);digest=hashlib.sha256(html.encode()).hexdigest()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_module_control'").fetchone():
            paused=c.execute("SELECT paused FROM pipeline_module_control WHERE module='seed'").fetchone()
            if paused and paused[0]:raise ValueError('seed_paused')
        seed=c.execute('SELECT body FROM source_discovery_seeds WHERE owner=? AND sku=?',(owner,sku)).fetchone()
        if not seed:raise ValueError('unknown_discovery_seed')
        if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone():raise ValueError('seed_blocked')
        if c.execute('SELECT 1 FROM source_discovery_receipts WHERE owner=? AND sku=? AND digest=?',(owner,sku,digest)).fetchone():return {'state':'replay','new_sellers':0}
        evidence=packet|{'artifact':artifact,'sha256':digest,'observed_at':now,'channel':'ozon-other-sellers-browser'}
        new=0
        for seller in packet['sellers']:
            new+=int(not c.execute('SELECT 1 FROM source_other_sellers WHERE owner=? AND seller=?',(owner,seller)).fetchone()
                     and not c.execute('SELECT 1 FROM browser_source_scans WHERE owner=? AND seller=?',(owner,seller)).fetchone())
            c.execute('INSERT INTO source_other_sellers VALUES(?,?,?,?,?) ON CONFLICT(owner,seed_sku,seller) DO UPDATE SET body=excluded.body,observed=excluded.observed',
                (owner,sku,seller,json.dumps(evidence),now))
        c.execute('INSERT INTO source_discovery_receipts VALUES(?,?,?,?,?)',(owner,sku,digest,json.dumps(evidence),now))
        state='complete' if packet['complete'] else 'partial'
        body=json.loads(seed[0]);body.update(last_result=evidence,consecutive_failures=0)
        c.execute('UPDATE source_discovery_seeds SET state=?,due=?,attempts=attempts+1,body=?,updated=? WHERE owner=? AND sku=?',
            (state,now+(86400 if packet['complete'] else 300),json.dumps(body),now,owner,sku))
    return {'state':state,'seller_count':len(packet['sellers']),'new_sellers':new,'expected':packet['expected']}

async def discover_one(db,owner,config,now=None):
    """Called under source-loop's profile lock; one bounded discovery per turn."""
    import asyncio,os
    from pathlib import Path
    now=time.time() if now is None else now
    with db.connect() as c:
        seed=c.execute("""SELECT * FROM source_discovery_seeds s WHERE owner=? AND due<=?
          AND state IN ('queued','partial','retry_wait','complete')
          AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.sku)
          ORDER BY updated,sku LIMIT 1""",(owner,now)).fetchone()
    if not seed:return {'state':'no_due_discovery'}
    root=Path(__file__).resolve().parents[1];process=None
    try:
        process=await asyncio.create_subprocess_exec('node',str(root/'bridges/other-sellers.mjs'),seed['sku'],
            str(root/'output/playwright/other-seller-expansion'),str(min(40,8*(seed['attempts']+1))),
            cwd=root,env=os.environ|{
                'FLOWHUB_SOURCE_PROFILE':config['profile'],
                **({'FLOWHUB_SOURCE_EXTENSION_DIR':str(config['extension_dir'])} if config.get('extension_dir') else {}),
                **({'FLOWHUB_SOURCE_CHROMIUM_EXECUTABLE':str(config['chromium_executable'])} if config.get('chromium_executable') else {}),
            },
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        output,error=await asyncio.wait_for(process.communicate(),120)
        if process.returncode:
            try:failure=json.loads(output)
            except (ValueError,TypeError):failure={}
            reason='browser_navigation_failed' if failure.get('network') or b'net::ERR_' in error else 'other_offers_unavailable_'+str(failure.get('error','unknown'))
            raise ValueError(reason)
        result=json.loads(output);artifact=Path(result['artifact'])
        return ingest(db,owner,seed['sku'],artifact.read_text(),str(artifact))|{'sku':seed['sku']}
    except Exception as error:
        reason=str(error) if isinstance(error,ValueError) else type(error).__name__
        attempts=seed['attempts']+1
        body=json.loads(seed['body']);failures=body.get('consecutive_failures',0)+1
        state='retry_wait' if failures<=3 and 'access_challenge' not in reason else 'blocked'
        with db.connect() as c:
            c.execute('INSERT INTO source_discovery_failures VALUES(?,?,?,?)',(owner,seed['sku'],reason,now))
            body.update(last_failure={'reason':reason,'at':now},consecutive_failures=failures)
            c.execute('UPDATE source_discovery_seeds SET state=?,due=?,attempts=?,body=?,updated=? WHERE owner=? AND sku=?',
                (state,now+min(21600,120*2**failures),attempts,json.dumps(body),now,owner,seed['sku']))
        return {'state':state,'sku':seed['sku'],'reason':reason}
    finally:
        if process and process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),10)
            except asyncio.TimeoutError:process.kill();await process.wait()


def prepare_samples(db,owner,limit=20):
    """New sellers enter one-page assessment, not automatic full-store approval."""
    from .browser_source import BrowserSource
    service=BrowserSource(db)
    with db.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS source_discovered_stores(owner TEXT,seller TEXT,run_id TEXT,state TEXT,
          body TEXT,updated REAL,PRIMARY KEY(owner,seller))''')
        c.execute("""UPDATE source_discovered_stores SET state='retry_wait',updated=?
          WHERE owner=? AND state='sampling' AND updated<?""",(time.time(),owner,time.time()-300))
        rows=c.execute('''SELECT s.* FROM source_other_sellers s WHERE owner=?
          AND NOT EXISTS(SELECT 1 FROM browser_source_scans b WHERE b.owner=s.owner AND b.seller=s.seller)
          AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.seed_sku)
          GROUP BY seller LIMIT ?''',(owner,limit)).fetchall()
    added=0
    for row in rows:
        run='other-seller-'+row['seller'];roots=[{'sku':row['seed_sku'],'channel':'other-seller-discovery','evidence':json.loads(row['body'])}]
        service.prepare(owner,run,{row['seller']:roots})
        # Only this just-created discovery scan is activated. Later manual
        # pauses are respected by sample_one's ready-state join.
        service.control(owner,run,row['seller'],'resume')
        with db.connect() as c:
            added+=c.execute('INSERT OR IGNORE INTO source_discovered_stores VALUES(?,?,?,?,?,?)',
                (owner,row['seller'],run,'pending_sample',json.dumps({'seed_sku':row['seed_sku']}),time.time())).rowcount
    return added

async def sample_one(db,owner,config,seller=None):
    from .browser_source import BrowserSource
    from .pipeline_modules.source_loop import collect
    with db.connect() as c:
        row=c.execute("""SELECT d.* FROM source_discovered_stores d JOIN browser_source_scans s
          ON s.owner=d.owner AND s.seller=d.seller AND s.run_id=d.run_id
          WHERE d.owner=? AND (? IS NULL OR d.seller=?) AND (
            (d.state='pending_sample' AND s.state='ready') OR
            (d.state='retry_wait' AND s.state IN ('ready','blocked') AND d.updated+300<=?))
          ORDER BY d.updated LIMIT 1""",(owner,seller,seller,time.time())).fetchone()
    if not row:return {'state':'no_due_sample'}
    s=BrowserSource(db);key=(owner,row['run_id'],row['seller'])
    if row['state']=='retry_wait' and s.next_request(*key)['state']=='blocked':s.control(*key,'retry')
    with db.connect() as c:c.execute("UPDATE source_discovered_stores SET state='sampling',updated=? WHERE owner=? AND seller=?",(time.time(),owner,row['seller']))
    previous=json.loads(row['body']);attempts=previous.get('sample_attempts',0)
    task=dict(row)|{'failures':attempts,'state':'ready'}
    result=await collect(db,task,config|{'pages_per_store':1})
    with db.connect() as c:
        page=c.execute('SELECT 1 FROM browser_source_pages WHERE owner=? AND run_id=? AND seller=?',key).fetchone()
        state='awaiting_qualified_product' if page else 'retry_wait' if result['state']=='retry_wait' else 'blocked'
        body=json.loads(row['body']);body.update(sample_result=result,sample_attempts=attempts+1)
        c.execute('UPDATE source_discovered_stores SET state=?,body=?,updated=? WHERE owner=? AND seller=?',
            (state,json.dumps(body),time.time(),owner,row['seller']))
    return {'state':state,'seller':row['seller']}


def promote_qualified(db,owner):
    """A sampled store needs a product passing existing source filters to expand."""
    with db.connect() as c:
        setting=c.execute('SELECT body FROM sourcing_settings WHERE owner=?',(owner,)).fetchone()
        filters=SourceFilters(**json.loads(setting[0])) if setting else SourceFilters();promoted=0
        for r in c.execute("SELECT * FROM source_discovered_stores WHERE owner=? AND state='awaiting_qualified_product'",(owner,)).fetchall():
            products=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND seller=?',(owner,r['seller'])).fetchall()
            qualified=next((json.loads(p[0]) for p in products if assess(json.loads(p[0]),filters)['state']=='qualified'),None)
            if not qualified:
                body=json.loads(r['body']);body['source_assessments']=[{'sku':json.loads(p[0])['sku'],**assess(json.loads(p[0]),filters)} for p in products[:8]]
                c.execute('UPDATE source_discovered_stores SET body=? WHERE owner=? AND seller=?',(json.dumps(body),owner,r['seller']))
                continue
            scan=c.execute('SELECT state FROM browser_source_scans WHERE owner=? AND seller=? AND run_id=?',(owner,r['seller'],r['run_id'])).fetchone()
            due=time.time()+86400 if scan and scan[0]=='done' else 0
            c.execute('INSERT OR IGNORE INTO source_loop_stores VALUES(?,?,?,?,0,?,?)',(owner,r['seller'],r['run_id'],due,'evaluated',time.time()))
            body=json.loads(r['body']);body['qualified_sku']=qualified['sku']
            c.execute("UPDATE source_discovered_stores SET state='qualified',body=?,updated=? WHERE owner=? AND seller=?",(json.dumps(body),time.time(),owner,r['seller']))
            promoted+=1
    return promoted
