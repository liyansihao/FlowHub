"""Free ERP favorite capacity only after archiving verified sold-source lineage."""
import asyncio
import fcntl
import hashlib
import json
import os
import time
import httpx
from pathlib import Path
from ..maozi import MaoziPublisher
from ..modules import ModuleError
from . import control
from .database_work import run as database_work

DEFAULTS={'enabled':False,'threshold_ratio':0.95,'target_ratio':2/3,'batch_size':1000,'interval_seconds':60,'minimum_age_seconds':3600,'max_checks_per_cycle':32,'cycle_seconds':120}


def config(db):
    path=Path(db.directory)/'favorite-cleanup.json'
    value=DEFAULTS|(json.loads(path.read_text()) if path.exists() else {})
    if not 0<value['target_ratio']<value['threshold_ratio']<=1:raise ValueError('invalid cleanup thresholds')
    if not 1<=value['batch_size']<=1000 or value['interval_seconds']<60 or value['minimum_age_seconds']<0:
        raise ValueError('invalid cleanup bounds')
    if not 1<=value['max_checks_per_cycle']<=100 or not 1<=value['cycle_seconds']<=300:raise ValueError('invalid cleanup work bounds')
    return value


def schema(db):
    with db.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS favorite_cleanup_receipts(
            account TEXT,favorite_id TEXT,owner TEXT,sku TEXT,state TEXT,body TEXT,updated REAL,
            PRIMARY KEY(account,favorite_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS favorite_cleanup_checks(
            account TEXT,work_key TEXT,last_checked REAL,next_check REAL,reason TEXT,
            PRIMARY KEY(account,work_key))''')


class Client:
    request_seconds=25
    def __init__(self,context):
        self.api=MaoziPublisher(context);self.requests=0;self.retries=0
    async def call(self,path,method='GET',params=None,body=None):
        # httpx's per-read timeout alone cannot bound a slowly streaming response.
        async with asyncio.timeout(self.request_seconds):
            for attempt in range(2 if method=='GET' else 1):
                self.requests+=1
                try:return await self.api.erp(method,path,params=params,body=body)
                except ValueError as e:raise FavoriteLookupError('invalid ERP response') from e
                except (httpx.TransportError,TimeoutError):
                    if method!='GET' or attempt:raise
                    self.retries+=1
                    await asyncio.sleep(1)


class FavoriteLookupError(ValueError):
    pass


def rows(response):return response if isinstance(response,list) else response.get('data',[])


class CleanupPaused(Exception):
    pass


async def listing(client,should_stop=lambda:False):
    found=[];header=None;total=None
    for page in range(1,101):
        if await database_work(should_stop):raise CleanupPaused()
        response=await client.call('/api.product.favorite/lists',params={'page':page,'page_size':100})
        if not isinstance(response,dict) or response.get('total') is None:raise ValueError('favorite total required')
        if header is None:header=response;total=int(response['total'])
        if total!=int(response['total']):raise ValueError('favorite list changed; retry next cycle')
        batch=rows(response)
        if not isinstance(batch,list):raise ValueError('invalid favorite list')
        found.extend(batch)
        if len(batch)<100 or len(found)>=total:
            if len(found)!=total or len({str(r['id']) for r in found})!=total:raise ValueError('incomplete favorite list')
            return header,found
    raise ValueError('favorite pagination exceeded')


async def exact_favorites(client,sku,should_stop=lambda:False):
    """The ERP SKU filter includes imported and unimported rows; never infer
    absence from a global, moving page. Reject ignored filters/truncation."""
    if await database_work(should_stop):raise CleanupPaused()
    response=await client.call('/api.product.favorite/lists',params={'sku':sku,'page':1,'page_size':100})
    if await database_work(should_stop):raise CleanupPaused()
    batch=rows(response) if isinstance(response,dict) else None
    total=response.get('total') if isinstance(response,dict) else None
    if not isinstance(batch,list) or not str(total).isdigit() or int(total)!=len(batch):
        raise FavoriteLookupError('incomplete exact favorite list')
    if any(not isinstance(r,dict) or str(r.get('sku'))!=str(sku) or not str(r.get('id','')).isdigit() for r in batch):
        raise FavoriteLookupError('exact favorite filter/identity mismatch')
    if len({str(r['id']) for r in batch})!=len(batch):raise FavoriteLookupError('duplicate favorite identity')
    return response,batch


def lineage(product,sku,seller):
    relation=product.get('source_relation') or {}
    return (str(product.get('sku'))==sku and str(product.get('seller_id'))==seller
            and str(relation.get('seller_id'))==seller and bool(relation.get('root_seeds')))


def candidates(db,owner,settings):
    with db.connect() as c:
        found=c.execute('''SELECT q.*,p.body product,s.config,s.secret FROM plugin_pipeline q
            JOIN sourcing_products p USING(owner,sku,seller)
            JOIN plugin_routes r USING(owner,sku,seller)
            JOIN stores s ON s.id=r.store_id AND s.owner=q.owner
            WHERE q.owner=? AND q.state='selling' AND q.due<=? ORDER BY q.due DESC''',
            (owner,time.time()-settings['minimum_age_seconds'])).fetchall()
    groups={}
    for r in found:
        product=json.loads(r['product']);queue=json.loads(r['body']);keys=db.open(r['secret'])
        if not lineage(product,r['sku'],r['seller']) or not queue.get('offer_id') or not keys.get('erp_token'):continue
        account=hashlib.sha256((owner+':'+keys['erp_token']).encode()).hexdigest()
        groups.setdefault(account,[]).append({'owner':owner,'sku':r['sku'],'seller':r['seller'],
            'offer_id':queue['offer_id'],'context':{'store':{'config':json.loads(r['config']),'credentials':keys}}})
    return groups


def pending_contexts(db,owner):
    # Receipts remain reconcilable even after their product leaves selling.
    with db.connect() as c:
        pending={r[0] for r in c.execute("SELECT DISTINCT account FROM favorite_cleanup_receipts WHERE owner=? AND state!='deleted'",(owner,))}
        stores=c.execute('SELECT config,secret FROM stores WHERE owner=?',(owner,)).fetchall()
    contexts={}
    for r in stores:
        keys=db.open(r['secret'])
        if not keys.get('erp_token'):continue
        account=hashlib.sha256((owner+':'+keys['erp_token']).encode()).hexdigest()
        if account in pending:contexts[account]={'store':{'config':json.loads(r['config']),'credentials':keys}}
    return contexts


def update_receipt(db,account,favorite,state,body):
    with db.connect() as c:
        c.execute('UPDATE favorite_cleanup_receipts SET state=?,body=?,updated=? WHERE account=? AND favorite_id=?',
                  (state,db.seal(body),time.time(),account,favorite))


def archive(db,c,item,favorite,online):
    """Two durable local copies, completed before any remote deletion."""
    key=(item['owner'],item['sku'],item['seller'])
    product=c.execute('SELECT * FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    if not product or not lineage(json.loads(product['body']),key[1],key[2]):return None
    q=c.execute('SELECT * FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    if not q or q['state']!='selling' or json.loads(q['body']).get('offer_id')!=item['offer_id']:return None
    if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(*key,time.time())).fetchone():return None
    source=json.loads(product['body']);roots=(source['source_relation']['root_seeds'])
    root_skus={str(r.get('sku') or r.get('source_sku')) for r in roots}
    seeds=[dict(r) for r in c.execute('SELECT * FROM sourcing_seeds WHERE owner=?',(key[0],)) if r['sku'] in root_skus]
    publication=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    if not publication or json.loads(publication['body']).get('offer_id')!=item['offer_id']:return None
    publication_body=json.loads(publication['body'])
    # Official API publication does not import an ERP favorite. Its immutable
    # completed intent plus the fresh online identity/stock check is the proof.
    if str(favorite.get('is_imported'))!='1' and not (
            publication_body.get('backend')=='official'
            and publication_body.get('phase')=='stock_verified'
            and publication_body.get('verified') is True):return None
    backup={'version':1,'at':time.time(),'owner':key[0],'sku':key[1],'seller':key[2],
            'favorite':favorite,'online':online,'product':dict(product),'root_seeds':roots,'seed_records':seeds,
            'publication':json.loads(publication['body']),'pipeline':dict(q),
            'evidence':[dict(r) for r in c.execute('SELECT * FROM sourcing_evidence WHERE owner=? AND sku=? ORDER BY hash',key[:2])],
            'scope':'ERP favorite only; local source graph and online listing retained'}
    directory=Path(db.directory)/'favorite-lineage-archive';directory.mkdir(mode=0o700,exist_ok=True)
    encoded=json.dumps(backup,ensure_ascii=False,sort_keys=True).encode();digest=hashlib.sha256(encoded).hexdigest()
    path=directory/(digest+'.json')
    if not path.exists():
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(encoded);f.flush();os.fsync(f.fileno())
        fd=os.open(directory,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('archive verification failed')
    return backup|{'archive_path':str(path),'archive_sha256':digest}


def receipt(db,account,favorite):
    with db.connect() as c:
        return c.execute('SELECT * FROM favorite_cleanup_receipts WHERE account=? AND favorite_id=?',
                         (account,favorite)).fetchone()


def prepare_archive(db,owner,account,item,favorite,online):
    # The durable archive and intent are one drained unit. Cancellation must not
    # release the account lock or reach the remote write while this is running.
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if control.paused(db,'seed') or not config(db)['enabled']:return 'paused',None
        if not c.execute('SELECT 1 FROM pipeline_campaigns p JOIN users u ON p.owner=u.id WHERE p.owner=? AND p.enabled=1 AND u.active=1',(owner,)).fetchone():return 'disabled',None
        fid=str(favorite['id'])
        if c.execute('SELECT 1 FROM favorite_cleanup_receipts WHERE account=? AND favorite_id=?',(account,fid)).fetchone():return 'exists',None
        backup=archive(db,c,item,favorite,online)
        if backup is None:return 'skipped',None
        inserted=c.execute('INSERT OR IGNORE INTO favorite_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
            (account,fid,owner,item['sku'],'intent',db.seal(backup),time.time())).rowcount
        return ('intent',backup) if inserted else ('exists',None)


def scheduled_work(db,account,items,prior,settings):
    """Persist fair rotation; failed/unchanged old products cannot monopolize a cycle."""
    work={}
    for r in prior:work['receipt:'+r['favorite_id']]={'kind':'receipt','key':'receipt:'+r['favorite_id'],'row':r}
    for item in items:work.setdefault('sku:'+item['sku'],{'kind':'candidate','key':'sku:'+item['sku'],'item':item})
    with db.connect() as c:
        checks={r['work_key']:r for r in c.execute('SELECT * FROM favorite_cleanup_checks WHERE account=?',(account,))}
        completed={r['sku']:r['last_at'] for r in c.execute("SELECT sku,max(updated) last_at FROM favorite_cleanup_receipts WHERE account=? AND state='deleted' GROUP BY sku",(account,))}
    now=time.time()
    due=[w for k,w in work.items() if k not in checks or checks[k]['next_check']<=now]
    # Completed receipts are prior observations, not proof of current absence.
    # New completed publications get service before thousands of archived SKUs.
    rank={k:i for i,k in enumerate(work)}
    due.sort(key=lambda w:(checks[w['key']]['last_checked'] if w['key'] in checks else completed.get(w.get('item',{}).get('sku'),0),rank[w['key']]))
    # Presence is a scheduling hint, never deletion evidence. Each item still
    # passes exact remote lookup, publication/stock checks and receipt guards.
    present_reasons={'not_imported','not_selling','online_identity_not_unique','no_stock','archive_skipped'}
    present=[];discovery=[];receipts=[]
    for w in due:
        if w['kind']=='receipt':receipts.append(w)
        elif w['key'] in checks and checks[w['key']]['reason'] in present_reasons:present.append(w)
        else:discovery.append(w)
    # Two presence checks, one discovery and one reconciliation per round.
    # Borrow unused candidate slots, retaining the original 3:1 split when
    # there are no presence observations. All lanes rotate by last_checked.
    selected=[];pi=di=ri=0
    limit=settings['max_checks_per_cycle']
    while len(selected)<limit and (pi<len(present) or di<len(discovery) or ri<len(receipts)):
        for preferred in ('present','present','discovery'):
            if len(selected)>=limit:break
            if pi<len(present) and (preferred=='present' or di>=len(discovery)):
                selected.append(present[pi]);pi+=1
            elif di<len(discovery):
                selected.append(discovery[di]);di+=1
        if ri<len(receipts) and len(selected)<limit:
            selected.append(receipts[ri]);ri+=1
    return selected,len(due)


def checked(db,account,key,reason,delay):
    now=time.time()
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO favorite_cleanup_checks VALUES(?,?,?,?,?)',
                  (account,key,now,now+delay,reason))


async def reconcile_one(db,account,r,client,stopped):
    _,remote=await exact_favorites(client,r['sku'],stopped)
    if r['favorite_id'] in {str(x['id']) for x in remote}:return 'unconfirmed_present'
    body=db.open(r['body']);body['absence_verified_at']=time.time()
    await database_work(update_receipt,db,account,r['favorite_id'],'deleted',body)
    return 'deleted'


async def clean_item(db,owner,account,item,settings,client,stopped,budget):
    sku=item['sku'];header,matches=await exact_favorites(client,sku,stopped)
    if not settings.get('clear_completed') and not str(header.get('used')).isdigit():raise FavoriteLookupError('account usage required')
    used=int(header.get('used') or 0);limit=int(header.get('limit') or 0)
    if budget['remaining'] is None:
        budget['remaining']=settings['batch_size'] if settings.get('clear_completed') else (min(settings['batch_size'],max(0,used-int(limit*settings['target_ratio']))) if limit>0 and used>=limit*settings['threshold_ratio'] else 0)
    if budget['remaining']<=0:return 'below_threshold'
    if len(matches)!=1:return 'favorite_absent' if not matches else 'favorite_ambiguous'
    favorite=matches[0];fid=str(favorite['id'])
    old=await database_work(receipt,db,account,fid)
    if old:return 'receipt_retained'  # Never replay an acknowledged/unknown mutation.
    shop=item['context']['store']['config']['shop_id'];offer=item['offer_id']
    data=await client.call('/api.product.online/lists',params={'page':1,'page_size':100,'shop_id':shop,'offer_id':offer})
    online_rows=rows(data) if isinstance(data,(dict,list)) else None
    if not isinstance(online_rows,list) or any(not isinstance(r,dict) for r in online_rows):raise FavoriteLookupError('invalid online rows')
    exact=[r for r in online_rows if str(r.get('shop_id'))==str(shop) and str(r.get('offer_id'))==str(offer)]
    if len(exact)!=1:return 'online_identity_not_unique'
    if exact[0].get('online_status')!='selling':return 'not_selling'
    try:stock=float(exact[0].get('stock') or 0)
    except (TypeError,ValueError):raise FavoriteLookupError('invalid online stock')
    if not 0<stock<float('inf'):return 'no_stock'
    state,backup=await database_work(prepare_archive,db,owner,account,item,favorite,exact[0])
    if state in ('paused','disabled'):raise CleanupPaused()
    if state!='intent':return 'archive_'+state
    budget['remaining']-=1
    try:
        backup['response']=await client.call('/api.product.favorite/toggle',method='POST',body={'status':False,'productInfo':favorite|{
            'sku':sku,'coverImage':favorite.get('cover_image',''),'price_info':{'sell_price':favorite.get('sell_price'),'currency':'CNY'}}})
    except (httpx.HTTPError,TimeoutError,ModuleError,FavoriteLookupError) as e:
        backup['error_type']=type(e).__name__
        await database_work(update_receipt,db,account,fid,'unconfirmed',backup)
        # A lost acknowledgement permits a read, never a second mutation.
    else:await database_work(update_receipt,db,account,fid,'acknowledged',backup)
    r=await database_work(receipt,db,account,fid)
    return await reconcile_one(db,account,r,client,stopped)


async def clean_account(db,owner,account,items,settings,client=None):
    client=client or Client(items[0]['context'])
    def stopped():return control.paused(db,'seed') or not config(db)['enabled']
    await database_work(schema,db)
    def previous_receipts():
        with db.connect() as c:return c.execute("SELECT * FROM favorite_cleanup_receipts WHERE account=? AND state!='deleted'",(account,)).fetchall()
    prior=await database_work(previous_receipts)
    work,due_count=await database_work(scheduled_work,db,account,items,prior,settings)
    result={'deleted':0,'skipped':0,'checked':0,'due_candidates':due_count,'reasons':{},'max_item_seconds':0}
    started=time.monotonic();budget={'remaining':None}
    for w in work:
        if await database_work(stopped):return result|{'state':'paused'}
        if result['deleted']>=settings['batch_size'] or time.monotonic()-started>=settings['cycle_seconds']:break
        # Reserve rotation before I/O. A crash delays this item, it cannot erase it.
        await database_work(checked,db,account,w['key'],'checking',60)
        item_started=time.monotonic()
        try:
            async with asyncio.timeout(min(40,max(.001,settings['cycle_seconds']-(item_started-started)))):
                if w['kind']=='receipt':reason=await reconcile_one(db,account,w['row'],client,stopped)
                else:reason=await clean_item(db,owner,account,w['item'],settings,client,stopped,budget)
        except CleanupPaused:return result|{'state':'paused'}
        except (httpx.HTTPError,TimeoutError,ModuleError,FavoriteLookupError) as e:
            reason='remote_'+type(e).__name__
        result['max_item_seconds']=round(max(result['max_item_seconds'],time.monotonic()-item_started),3)
        result['checked']+=1;result['reasons'][reason]=result['reasons'].get(reason,0)+1
        if reason=='deleted':
            result['deleted']+=1
        else:result['skipped']+=1
        await database_work(checked,db,account,w['key'],reason,300 if reason!='deleted' else 3600)
    if await database_work(stopped):return result|{'state':'paused'}
    return result|{'state':'cleaned' if result['deleted'] else ('below_threshold' if result['reasons'] and set(result['reasons'])=={'below_threshold'} else 'no_safe_candidates'),
                   'duration_seconds':round(time.monotonic()-started,3),'requests':getattr(client,'requests',None),'read_retries':getattr(client,'retries',None)}


async def tick(db):
    settings=config(db)
    if not settings['enabled'] or await database_work(control.paused,db,'seed'):return []
    await database_work(schema,db)
    with (Path(db.directory)/'favorite-cleanup.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return []
        def active_owners():
            with db.connect() as c:return [r[0] for r in c.execute('SELECT p.owner FROM pipeline_campaigns p JOIN users u ON p.owner=u.id WHERE p.enabled=1 AND u.active=1')]
        owners=await database_work(active_owners)
        results=[]
        for owner in owners:
            groups=await database_work(candidates,db,owner,settings)
            contexts=await database_work(pending_contexts,db,owner)
            for account in sorted(set(groups)|set(contexts)):
                items=groups.get(account,[])
                if await database_work(control.paused,db,'seed') or not config(db)['enabled']:return results
                started=time.time()
                try:result=await clean_account(db,owner,account,items,settings,Client(items[0]['context'] if items else contexts[account]))
                except Exception as e:result={'state':'error','error_type':type(e).__name__,'reason':str(e)[:160] if isinstance(e,ValueError) else type(e).__name__}
                await database_work(control.record,db,'seed',owner,'',started,'favorite_cleanup',result);results.append(result)
        return results


async def run(db):
    while True:
        try:await tick(db)
        except Exception as e:await database_work(control.record,db,'seed','','',time.time(),'favorite_cleanup_error',{'error_type':type(e).__name__,'reason':str(e)[:160] if isinstance(e,ValueError) else type(e).__name__})
        await asyncio.sleep(config(db)['interval_seconds'])
