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

DEFAULTS={'enabled':False,'threshold_ratio':0.85,'target_ratio':0.80,'batch_size':20,'interval_seconds':300,'minimum_age_seconds':3600}


def schema(db):
    with db.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS draft_cleanup_receipts(
            account TEXT,draft_id TEXT,owner TEXT,sku TEXT,state TEXT,body TEXT,updated REAL,
            PRIMARY KEY(account,draft_id))''')


def config(db):
    p=Path(db.directory)/'draft-cleanup.json'
    d=DEFAULTS|json.loads(p.read_text()) if p.exists() else dict(DEFAULTS)
    if not 0<d['target_ratio']<d['threshold_ratio']<=1:raise ValueError('invalid cleanup thresholds')
    if not 1<=d['batch_size']<=20 or d['minimum_age_seconds']<3600 or d['interval_seconds']<60:
        raise ValueError('invalid cleanup limits')
    return d


class Client:
    def __init__(self,context):self.api=MaoziPublisher(context)
    async def call(self,path,method='GET',params=None):
        # No automatic write retry. Every deletion is journaled first.
        try:return await self.api.erp(method,path,params=params)
        finally:await asyncio.sleep(2.5)


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
        found=c.execute('''SELECT q.sku,q.seller,q.body,s.config,s.secret FROM plugin_pipeline q
            JOIN plugin_routes r USING(owner,sku,seller) JOIN stores s ON s.id=r.store_id AND s.owner=q.owner
            WHERE q.owner=? AND q.state='selling' ORDER BY q.due''',(owner,)).fetchall()
    groups={}
    for r in found:
        ctx={'owner':owner,'candidate':{'source_key':r['sku']},'store':{'config':json.loads(r['config']),'credentials':db.open(r['secret'])}}
        token=ctx['store']['credentials'].get('erp_token')
        if not token:continue
        collector=SourceCollector(db,ctx);state,snapshot,updated=collector.load()
        if (state!='ready' or not snapshot.get('draft_id') or not snapshot.get('favorite_id')
                or snapshot.get('recovery') or not snapshot.get('detail') or str(snapshot.get('source_key'))!=r['sku']
                or time.time()-updated<settings['minimum_age_seconds']):continue
        account=hashlib.sha256((owner+':'+token).encode()).hexdigest()
        groups.setdefault(account,[]).append({'sku':r['sku'],'seller':r['seller'],'queue':json.loads(r['body']),
            'snapshot':snapshot,'context':ctx,'source_record':collector.key})
    return groups


async def clean_account(db,owner,account,items,settings,client=None):
    client=client or Client(items[0]['context'])
    header,remote=await listing(client)
    present={str(r['id']):r for r in remote}
    # Unknown prior writes are reconciled by absence only, never replayed.
    with db.connect() as c:prior=c.execute('SELECT * FROM draft_cleanup_receipts WHERE account=?',(account,)).fetchall()
    for r in prior:
        if r['state']!='deleted' and r['draft_id'] not in present:
            body=db.open(r['body']);body['absence_verified_at']=time.time()
            journal(db,account,r['draft_id'],owner,r['sku'],'deleted',body)
    used=int(header.get('used',header.get('total',0)));limit=int(header.get('limit',0))
    summary={'used_before':used,'limit':limit,'deleted':0,'skipped':0}
    if limit<=0 or used<limit*settings['threshold_ratio']:return summary|{'state':'below_threshold'}
    count=min(settings['batch_size'],max(0,used-int(limit*settings['target_ratio'])))
    touched=[];seen=set()
    for item in items:
        draft=str(item['snapshot']['draft_id']);sku=item['sku'];row=present.get(draft)
        if draft in seen:continue
        seen.add(draft)
        if len(touched)>=count:break
        if not row or str(row.get('goods_id'))!=sku or row.get('collect_from')!='ozon':continue
        with db.connect() as c:
            if c.execute('SELECT 1 FROM draft_cleanup_receipts WHERE account=? AND draft_id=?',(account,draft)).fetchone():continue
        # Verify exact online offer/shop using the candidate's own route.
        current=client
        shop=item['context']['store']['config']['shop_id'];offer=item['queue'].get('offer_id')
        if not offer:continue
        data=await current.call('/api.product.online/lists',params={'page':1,'page_size':100,'shop_id':shop,'offer_id':offer})
        exact=[r for r in rows(data) if str(r.get('shop_id'))==str(shop) and str(r.get('offer_id'))==str(offer)]
        if len(exact)!=1 or exact[0].get('online_status')!='selling' or float(exact[0].get('stock') or 0)<=0:
            summary['skipped']+=1;continue
        detail=await current.call('/api.product.collect/detail',params={'id':int(draft),'is_online':0})
        if not isinstance(detail,dict) or not detail.get('skus'):continue
        backup={'source_snapshot':item['snapshot'],'fresh_detail':detail,'draft_row':row,'online':exact[0],
                'at':time.time(),'source_record':item['source_record'],'scope':'source draft only'}
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            q=c.execute('SELECT state,body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',(owner,sku,item['seller'])).fetchone()
            if not q or q['state']!='selling' or json.loads(q['body']).get('offer_id')!=offer:continue
            if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,item['seller'],time.time())).fetchone():continue
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
        after,remote=await listing(client);remaining={str(r['id']) for r in remote}
        for draft in touched:
            if draft in remaining:continue
            with db.connect() as c:r=c.execute('SELECT * FROM draft_cleanup_receipts WHERE account=? AND draft_id=?',(account,draft)).fetchone()
            body=db.open(r['body']);body['absence_verified_at']=time.time()
            journal(db,account,draft,owner,r['sku'],'deleted',body);summary['deleted']+=1
        summary['used_after']=after.get('used')
    return summary|{'state':'cleaned' if summary['deleted'] else 'no_safe_candidates'}


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
                started=time.time()
                try:result=await clean_account(db,owner,account,items,settings)
                except Exception as error:result={'state':'error','error_type':type(error).__name__}
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
