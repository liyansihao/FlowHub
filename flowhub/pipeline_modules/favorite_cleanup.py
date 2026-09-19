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
from . import control
from .database_work import run as database_work

DEFAULTS={'enabled':False,'threshold_ratio':0.95,'target_ratio':2/3,'batch_size':1000,'interval_seconds':60,'minimum_age_seconds':3600}


def config(db):
    path=Path(db.directory)/'favorite-cleanup.json'
    value=DEFAULTS|(json.loads(path.read_text()) if path.exists() else {})
    if not 0<value['target_ratio']<value['threshold_ratio']<=1:raise ValueError('invalid cleanup thresholds')
    if not 1<=value['batch_size']<=1000 or value['interval_seconds']<60 or value['minimum_age_seconds']<0:
        raise ValueError('invalid cleanup bounds')
    return value


def schema(db):
    with db.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS favorite_cleanup_receipts(
            account TEXT,favorite_id TEXT,owner TEXT,sku TEXT,state TEXT,body TEXT,updated REAL,
            PRIMARY KEY(account,favorite_id))''')


class Client:
    def __init__(self,context):self.api=MaoziPublisher(context)
    async def call(self,path,method='GET',params=None,body=None):
        try:
            for attempt in range(2 if method=='GET' else 1):
                try:return await self.api.erp(method,path,params=params,body=body)
                except (httpx.TransportError,TimeoutError):
                    if method!='GET' or attempt:raise
                    await asyncio.sleep(1)
        finally:await asyncio.sleep(.2)


def rows(response):return response if isinstance(response,list) else response.get('data',[])


class CleanupPaused(Exception):
    pass


async def listing(client,should_stop=lambda:False):
    found=[];header=None;total=None
    for page in range(1,101):
        if should_stop():raise CleanupPaused()
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
            WHERE q.owner=? AND q.state='selling' AND q.due<=? ORDER BY q.due''',
            (owner,time.time()-settings['minimum_age_seconds'])).fetchall()
    groups={}
    for r in found:
        product=json.loads(r['product']);queue=json.loads(r['body']);keys=db.open(r['secret'])
        if not lineage(product,r['sku'],r['seller']) or not queue.get('offer_id') or not keys.get('erp_token'):continue
        account=hashlib.sha256((owner+':'+keys['erp_token']).encode()).hexdigest()
        groups.setdefault(account,[]).append({'owner':owner,'sku':r['sku'],'seller':r['seller'],
            'offer_id':queue['offer_id'],'context':{'store':{'config':json.loads(r['config']),'credentials':keys}}})
    return groups


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
    backup={'version':1,'at':time.time(),'owner':key[0],'sku':key[1],'seller':key[2],
            'favorite':favorite,'online':online,'product':dict(product),'root_seeds':roots,'seed_records':seeds,
            'publication':json.loads(publication['body']),'pipeline':dict(q),
            'evidence':[dict(r) for r in c.execute('SELECT * FROM sourcing_evidence WHERE owner=? AND sku=?',key[:2])],
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


async def clean_account(db,owner,account,items,settings,client=None):
    client=client or Client(items[0]['context'])
    def stopped():return control.paused(db,'seed') or not config(db)['enabled']
    try:header,remote=await listing(client,stopped)
    except CleanupPaused:return {'state':'paused','deleted':0,'skipped':0}
    present={str(r['id']):r for r in remote}
    def previous_receipts():
        with db.connect() as c:return c.execute('SELECT * FROM favorite_cleanup_receipts WHERE account=?',(account,)).fetchall()
    prior=await database_work(previous_receipts)
    # A timeout is reconciled by absence; a still-present favorite is never blindly deleted again.
    for r in prior:
        if r['state']!='deleted' and r['favorite_id'] not in present:
            body=db.open(r['body']);body['absence_verified_at']=time.time()
            update_receipt(db,account,r['favorite_id'],'deleted',body)
    used=int(header.get('used',header['total']));limit=int(header.get('limit') or 0)
    result={'used_before':used,'limit':limit,'deleted':0,'skipped':0}
    if not settings.get('clear_completed') and (limit<=0 or used<limit*settings['threshold_ratio']):return result|{'state':'below_threshold'}
    count=min(settings['batch_size'],len(items) if settings.get('clear_completed') else max(0,used-int(limit*settings['target_ratio'])))
    touched=[]
    for item in items:
        if stopped():return result|{'state':'paused'}
        if len(touched)>=count:break
        sku=item['sku'];matches=[r for r in remote if str(r.get('sku'))==sku]
        if len(matches)!=1 or str(matches[0].get('is_imported'))!='1':continue
        favorite=matches[0];fid=str(favorite['id'])
        with db.connect() as c:
            if c.execute('SELECT 1 FROM favorite_cleanup_receipts WHERE account=? AND favorite_id=?',(account,fid)).fetchone():continue
        shop=item['context']['store']['config']['shop_id'];offer=item['offer_id']
        try:
            data=await client.call('/api.product.online/lists',params={'page':1,'page_size':100,'shop_id':shop,'offer_id':offer})
        except Exception:
            result['skipped']+=1;continue
        exact=[r for r in rows(data) if str(r.get('shop_id'))==str(shop) and str(r.get('offer_id'))==str(offer)]
        if len(exact)!=1 or exact[0].get('online_status')!='selling' or float(exact[0].get('stock') or 0)<=0:
            result['skipped']+=1;continue
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if control.paused(db,'seed') or not config(db)['enabled']:return result|{'state':'paused'}
            if not c.execute('SELECT 1 FROM pipeline_campaigns p JOIN users u ON p.owner=u.id WHERE p.owner=? AND p.enabled=1 AND u.active=1',(owner,)).fetchone():return result|{'state':'disabled'}
            backup=archive(db,c,item,favorite,exact[0])
            if backup is None:result['skipped']+=1;continue
            inserted=c.execute('INSERT OR IGNORE INTO favorite_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
                (account,fid,owner,sku,'intent',db.seal(backup),time.time())).rowcount
            if not inserted:continue
        touched.append(fid)
        try:
            backup['response']=await client.call('/api.product.favorite/toggle',method='POST',body={'status':False,'productInfo':favorite|{
                'sku':sku,'coverImage':favorite.get('cover_image',''),'price_info':{'sell_price':favorite.get('sell_price'),'currency':'CNY'}}})
            update_receipt(db,account,fid,'acknowledged',backup)
        except Exception as e:
            backup['error_type']=type(e).__name__;update_receipt(db,account,fid,'unconfirmed',backup);break
    if touched:
        try:after,remote=await listing(client,stopped)
        except CleanupPaused:return result|{'state':'paused'}
        remaining={str(r['id']) for r in remote}
        for fid in touched:
            if fid in remaining:continue
            with db.connect() as c:r=c.execute('SELECT body FROM favorite_cleanup_receipts WHERE account=? AND favorite_id=?',(account,fid)).fetchone()
            backup=db.open(r['body']);backup['absence_verified_at']=time.time()
            update_receipt(db,account,fid,'deleted',backup);result['deleted']+=1
        result['used_after']=after.get('used',after['total'])
    return result|{'state':'cleaned' if result['deleted'] else 'no_safe_candidates'}


async def tick(db):
    settings=config(db)
    if not settings['enabled'] or control.paused(db,'seed'):return []
    schema(db)
    with (Path(db.directory)/'favorite-cleanup.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return []
        with db.connect() as c:owners=[r[0] for r in c.execute('SELECT p.owner FROM pipeline_campaigns p JOIN users u ON p.owner=u.id WHERE p.enabled=1 AND u.active=1')]
        results=[]
        for owner in owners:
            for account,items in (await database_work(candidates,db,owner,settings)).items():
                if control.paused(db,'seed') or not config(db)['enabled']:return results
                started=time.time()
                try:result=await clean_account(db,owner,account,items,settings)
                except Exception as e:result={'state':'error','error_type':type(e).__name__,'reason':str(e)[:160] if isinstance(e,ValueError) else type(e).__name__}
                control.record(db,'seed',owner,'',started,'favorite_cleanup',result);results.append(result)
        return results


async def run(db):
    while True:
        try:await tick(db)
        except Exception as e:control.record(db,'seed','','',time.time(),'favorite_cleanup_error',{'error_type':type(e).__name__,'reason':str(e)[:160] if isinstance(e,ValueError) else type(e).__name__})
        await asyncio.sleep(config(db)['interval_seconds'])
