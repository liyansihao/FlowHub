"""Bounded, backed-up cleanup of this workflow's sold source drafts only."""
import asyncio
import fcntl
import hashlib
import json
import time
from pathlib import Path
from ..maozi import MaoziPublisher
from ..source_detail import SourceCollector
from . import control

DEFAULTS={'enabled':False,'threshold_ratio':0.85,'target_ratio':0.80,'batch_size':20,'scan_limit':20,'interval_seconds':300,'minimum_age_seconds':3600}


def schema(db):
    with db.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS draft_cleanup_receipts(
            account TEXT,draft_id TEXT,owner TEXT,sku TEXT,state TEXT,body TEXT,updated REAL,
            PRIMARY KEY(account,draft_id))''')
        c.execute('CREATE TABLE IF NOT EXISTS draft_cleanup_cursors(account TEXT PRIMARY KEY,draft_id TEXT,updated REAL)')


def config(db):
    p=Path(db.directory)/'draft-cleanup.json'
    d=DEFAULTS|json.loads(p.read_text()) if p.exists() else dict(DEFAULTS)
    if not 0<d['target_ratio']<d['threshold_ratio']<=1:raise ValueError('invalid cleanup thresholds')
    if not 1<=d['batch_size']<=1000 or not 1<=d['scan_limit']<=100 or d['minimum_age_seconds']<0 or d['interval_seconds']<60:
        raise ValueError('invalid cleanup limits')
    return d


class Client:
    def __init__(self,context):self.api=MaoziPublisher(context)
    async def call(self,path,method='GET',params=None):
        # No automatic write retry. Every deletion is journaled first.
        try:return await self.api.erp(method,path,params=params)
        finally:await asyncio.sleep(.2)


def rows(data):return data if isinstance(data,list) else data.get('data',[])


async def listing(client):
    found=[];first=None;expected_total=None
    for page in range(1,21):
        d=await client.call('/api.product.collect/lists',params={'page':page,'page_size':100})
        if first is None:first=d
        batch=rows(d)
        if not isinstance(batch,list):raise ValueError('invalid draft list')
        found.extend(batch)
        total=d.get('total') if isinstance(d,dict) else None
        if total is not None:
            if expected_total is None:expected_total=int(total)
            elif int(total)!=expected_total:raise ValueError('draft listing changed during pagination')
        if len(batch)<100 or (total is not None and len(found)>=int(total)):
            unique={str(r['id']) for r in found}
            if expected_total is not None and len(unique)!=expected_total:
                raise ValueError('draft listing unstable; absence unproven')
            return first,found
    raise ValueError('draft listing incomplete; no deletion')


def journal(db,account,draft_id,owner,sku,state,body):
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO draft_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
                  (account,str(draft_id),owner,sku,state,db.seal(body),time.time()))


def candidates(db,owner,settings):
    with db.connect() as c:
        found=c.execute('''SELECT q.sku,q.seller,q.body,s.id AS store_id,s.config,s.secret FROM plugin_pipeline q
            JOIN plugin_routes r USING(owner,sku,seller) JOIN stores s ON s.id=r.store_id AND s.owner=q.owner
            WHERE q.owner=? AND q.state='selling' ORDER BY q.due''',(owner,)).fetchall()
    groups={};snapshot_index=None
    for r in found:
        ctx={'owner':owner,'candidate':{'source_key':r['sku']},'store':{'config':json.loads(r['config']),'credentials':db.open(r['secret'])}}
        token=ctx['store']['credentials'].get('erp_token')
        if not token:continue
        collector=SourceCollector(db,ctx);state,snapshot,updated=collector.load()
        # Credential rotation changes the collector cache key. The immutable,
        # owner/SKU/seller-bound publication retains the exact source draft used.
        # Never search another owner's unbound source cache by SKU alone.
        if state!='ready' or not snapshot.get('draft_id'):
            with db.connect() as c:
                exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone()
                row=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',
                              (owner,r['sku'],r['seller'])).fetchone() if exists else None
            record=json.loads(row[0]) if row else {}
            saved=record.get('snapshot') or {}
            queue=json.loads(r['body'])
            if (record.get('owner')==owner and record.get('store_id')==r['store_id'] and str(record.get('sku'))==r['sku']
                    and str(record.get('seller'))==r['seller'] and record.get('verified') is True
                    and queue.get('offer_id') and record.get('offer_id')==queue['offer_id']):
                if not saved.get('draft_id'):
                    steps=record.get('review',{}).get('candidate',{}).get('origin',{}).get('collection_evidence',{}).get('last_repair',{}).get('steps',[])
                    draft_ids={str(s['draft_id']) for s in steps if s.get('draft_id') and s.get('source') in ('maozi-erp-draft','maozi-erp-draft-cache')}
                    if draft_ids:
                        if snapshot_index is None:
                            snapshot_index={}
                            with db.connect() as c:
                                for source in c.execute("SELECT key,body,updated FROM source_details WHERE state='ready'"):
                                    value=db.open(source['body'])
                                    if value.get('draft_id'):
                                        snapshot_index.setdefault((str(value.get('source_key')),str(value['draft_id'])),[]).append((value,source['updated']))
                        linked=[v for draft in draft_ids for v in snapshot_index.get((r['sku'],draft),[])]
                        if len(linked)==1:saved=linked[0][0]
                if str(saved.get('source_key'))==r['sku']:
                    state,snapshot,updated='ready',saved,record.get('started_at',time.time())
        if (state!='ready' or not snapshot.get('draft_id') or not snapshot.get('favorite_id')
                or (snapshot.get('recovery') and not settings.get('clear_completed')) or not snapshot.get('detail') or str(snapshot.get('source_key'))!=r['sku']
                or time.time()-updated<settings['minimum_age_seconds']):continue
        account=hashlib.sha256((owner+':'+token).encode()).hexdigest()
        groups.setdefault(account,[]).append({'sku':r['sku'],'seller':r['seller'],'queue':json.loads(r['body']),
            'snapshot':snapshot,'context':ctx,'source_record':collector.key})
    return groups


async def sold_observation(db,owner,item,client):
    """Read sale and stock from the backend that actually published this offer."""
    from dataclasses import asdict
    key=(owner,item['sku'],item['seller']);offer=item['queue'].get('offer_id')
    if not offer:return None
    with db.connect() as c:
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone()
        row=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',key).fetchone() if exists else None
    record=json.loads(row[0]) if row else {}
    cfg=item['context']['store']['config'];shop=str(cfg['shop_id'])
    if record.get('backend')=='official' or item['queue'].get('backend')=='official':
        from ..plugin_publication import classify_product_issues
        from ..official_status import OzonSellerStatusAdapter
        from .. import official_api
        keys=item['context']['store'].get('credentials') or {}
        if (record.get('backend')!='official' or record.get('offer_id')!=offer
            or record.get('account_binding')!=[str(keys.get('client_id')),str(cfg.get('warehouse_id')),shop]
            or not keys.get('api_key')):return None
        async with official_api.client(db,keys) as api:
            port=OzonSellerStatusAdapter(api,shop_id=shop,warehouse_id=str(cfg['warehouse_id']))
            await port.verify_identity()
            product=await port.find_product(shop,offer)
            if (not product or product.status!='selling' or product.sku=='0'
                or classify_product_issues(product.issue_codes,status=product.status)):return None
            stocks=await port.read_stocks(product)
            if not any(s.warehouse_id==str(cfg['warehouse_id']) and s.present>0 for s in stocks):return None
            return {'backend':'official','product':asdict(product),'stocks':[asdict(s) for s in stocks],
                    'account_binding':record['account_binding']}
    data=await client.call('/api.product.online/lists',params={'page':1,'page_size':100,'shop_id':cfg['shop_id'],'offer_id':offer})
    exact=[r for r in rows(data) if str(r.get('shop_id'))==shop and str(r.get('offer_id'))==str(offer)]
    if len(exact)!=1 or exact[0].get('online_status')!='selling' or float(exact[0].get('stock') or 0)<=0:return None
    return exact[0]


async def clean_account(db,owner,account,items,settings,client=None):
    client=client or Client(items[0]['context'])
    read_started=time.time()
    header,remote=await listing(client)
    from ..collection_capacity import observe
    observe(db,account,header,read_started)
    present={str(r['id']):r for r in remote}
    # Unknown prior writes are reconciled by absence only, never replayed.
    with db.connect() as c:prior=c.execute('SELECT * FROM draft_cleanup_receipts WHERE account=?',(account,)).fetchall()
    for r in prior:
        if r['state']!='deleted' and r['draft_id'] not in present:
            body=db.open(r['body']);body['absence_verified_at']=time.time()
            journal(db,account,r['draft_id'],owner,r['sku'],'deleted',body)
    used=int(header.get('used',header.get('total',0)));limit=int(header.get('limit',0))
    summary={'used_before':used,'limit':limit,'deleted':0,'skipped':0}
    if not settings.get('clear_completed') and (limit<=0 or used<limit*settings['threshold_ratio']):return summary|{'state':'below_threshold'}
    count=min(settings['batch_size'],len(items) if settings.get('clear_completed') else max(0,used-int(limit*settings['target_ratio'])))
    touched=[];seen=set();examined=0
    with db.connect() as c:
        cursor=c.execute('SELECT draft_id FROM draft_cleanup_cursors WHERE account=?',(account,)).fetchone()
    ordered=sorted(items,key=lambda item:str(item['snapshot']['draft_id']))
    if cursor:ordered=[i for i in ordered if str(i['snapshot']['draft_id'])>cursor[0]]+[i for i in ordered if str(i['snapshot']['draft_id'])<=cursor[0]]
    for item in ordered:
        if control.paused(db,'seed') or not config(db)['enabled']:return summary|{'state':'paused'}
        draft=str(item['snapshot']['draft_id']);sku=item['sku'];row=present.get(draft)
        if draft in seen:continue
        seen.add(draft)
        if len(touched)>=count:break
        if not row or str(row.get('goods_id'))!=sku or row.get('collect_from')!='ozon':continue
        with db.connect() as c:
            if c.execute('SELECT 1 FROM draft_cleanup_receipts WHERE account=? AND draft_id=?',(account,draft)).fetchone():continue
        if examined>=settings.get('scan_limit',20):break
        examined+=1
        with db.connect() as c:
            c.execute('INSERT OR REPLACE INTO draft_cleanup_cursors VALUES(?,?,?)',(account,draft,time.time()))
        current=client;offer=item['queue'].get('offer_id')
        try:online=await sold_observation(db,owner,item,current)
        except Exception:
            summary['skipped']+=1;continue
        if online is None:
            summary['skipped']+=1;continue
        try:
            detail=await current.call('/api.product.collect/detail',params={'id':int(draft),'is_online':0})
        except Exception:
            summary['skipped']+=1;continue
        if not isinstance(detail,dict) or not detail.get('skus'):continue
        backup={'source_snapshot':item['snapshot'],'fresh_detail':detail,'draft_row':row,'online':online,
                'at':time.time(),'source_record':item['source_record'],'scope':'source draft only'}
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            from ..favorite_retention import draft_retained
            if draft_retained(c,draft):continue
            q=c.execute('SELECT state,body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',(owner,sku,item['seller'])).fetchone()
            if not q or q['state']!='selling' or json.loads(q['body']).get('offer_id')!=offer:continue
            if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,item['seller'],time.time())).fetchone():continue
            if c.execute("SELECT 1 FROM plugin_pipeline WHERE owner=? AND sku=? AND state IN ('queued','evaluating','needs_fields','publishing','awaiting_remote','delisting')",(owner,sku)).fetchone():continue
            if control.paused(db,'seed') or not config(db)['enabled']:return summary|{'state':'paused'}
            if not c.execute('SELECT 1 FROM pipeline_campaigns WHERE owner=? AND enabled=1',(owner,)).fetchone():return summary|{'state':'disabled'}
            inserted=c.execute('INSERT OR IGNORE INTO draft_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
                (account,draft,owner,sku,'intent',db.seal(backup),time.time())).rowcount
            if not inserted:continue
        touched.append(draft)
        try:
            backup['response']=await current.call('/api.product.collect/del','DELETE',{'ids':draft})
            journal(db,account,draft,owner,sku,'acknowledged',backup)
        except Exception as error:
            backup['error_type']=type(error).__name__
            journal(db,account,draft,owner,sku,'unconfirmed',backup)
            break
    if touched:
        read_started=time.time()
        after,remote=await listing(client);remaining={str(r['id']) for r in remote}
        observe(db,account,after,read_started)
        for draft in touched:
            if draft in remaining:continue
            with db.connect() as c:r=c.execute('SELECT * FROM draft_cleanup_receipts WHERE account=? AND draft_id=?',(account,draft)).fetchone()
            body=db.open(r['body']);body['absence_verified_at']=time.time()
            journal(db,account,draft,owner,r['sku'],'deleted',body);summary['deleted']+=1
        summary['used_after']=after.get('used')
    return summary|{'state':'cleaned' if summary['deleted'] else 'no_safe_candidates','examined':examined}


async def tick(db):
    settings=config(db)
    if not settings['enabled'] or control.paused(db,'seed'):return []
    schema(db)
    with (Path(db.directory)/'draft-cleanup.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return []
        with db.connect() as c:owners=[r[0] for r in c.execute('SELECT p.owner FROM pipeline_campaigns p JOIN users u ON u.id=p.owner WHERE p.enabled=1 AND u.active=1')]
        results=[]
        for owner in owners:
            for account,items in candidates(db,owner,settings).items():
                with db.connect() as c:
                    c.execute('CREATE TABLE IF NOT EXISTS draft_cleanup_backoff(account TEXT PRIMARY KEY,failures INTEGER,due REAL)')
                    backoff=c.execute('SELECT failures,due FROM draft_cleanup_backoff WHERE account=?',(account,)).fetchone()
                if backoff and backoff['due']>time.time():continue
                started=time.time()
                try:result=await clean_account(db,owner,account,items,settings)
                except Exception as error:result={'state':'error','error_type':type(error).__name__}
                failures=(backoff['failures'] if backoff else 0)+1 if result['state'] in ('no_safe_candidates','error') else 0
                delay=min(1800,settings['interval_seconds']*2**min(failures,5)) if failures else settings['interval_seconds']
                with db.connect() as c:
                    c.execute('INSERT OR REPLACE INTO draft_cleanup_backoff VALUES(?,?,?)',(account,failures,time.time()+delay))
                result.update(consecutive_no_progress=failures,next_scan_after_seconds=delay)
                control.record(db,'seed',owner,'',started,'draft_cleanup',result);results.append(result)
        return results


async def run(db):
    while True:
        interval=300
        try:
            await tick(db)
            interval=config(db)['interval_seconds']
        except Exception as error:
            control.record(db,'seed','','',time.time(),'draft_cleanup_error',{'error_type':type(error).__name__})
        await asyncio.sleep(interval)
