"""Narrow release of completed favorites backed by retained independent drafts.

This is not a classifier for unknown history. A retained draft is a live refresh
source, not a license to treat an old snapshot as fresh business evidence.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import time
from pathlib import Path
from ..modules import Pending


@contextmanager
def source_guard(directory, sku, busy=Pending):
    directory=Path(directory)/'favorite-dependency-locks'
    directory.mkdir(exist_ok=True)
    with (directory/(hashlib.sha256(str(sku).encode()).hexdigest()+'.lock')).open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise busy('favorite dependency operation in progress') from None
        yield


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS favorite_release_certificates(
        account TEXT,favorite_id TEXT,sku TEXT,source_key TEXT,draft_id TEXT,
        state TEXT,proof TEXT,updated REAL,PRIMARY KEY(account,favorite_id))''')
    c.execute('CREATE INDEX IF NOT EXISTS favorite_release_draft ON favorite_release_certificates(draft_id)')


def draft_retained(c,draft_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='favorite_release_certificates'").fetchone():return False
    return bool(c.execute('SELECT 1 FROM favorite_release_certificates WHERE draft_id=?',(str(draft_id),)).fetchone())


def evaluate(publications, queues, *, source, favorite_id, jobs=(), acquisition=(), leases=(), draft_receipts=(), favorite_receipts=()):
    """Pure fail-closed decision. Caller supplies ALL matching SKU consumers."""
    if not publications or not queues:return 'missing_consumers'
    if acquisition:return 'other_consumer'
    if leases:return 'active_lease'
    if draft_receipts:return 'draft_deletion_record'
    if favorite_receipts:return 'prior_favorite_write'
    if source.get('state')!='ready':return 'source_not_ready'
    if str(source.get('favorite_id'))!=str(favorite_id):return 'favorite_identity'
    if not source.get('draft_id') or not isinstance(source.get('detail'),dict) or not source['detail'].get('skus'):return 'incomplete_snapshot'
    if any(p.get('phase')!='stock_verified' or p.get('verified') is not True or not p.get('offer_id') or not p.get('store_id') for p in publications):return 'publication_not_complete'
    pkeys={(p['owner'],p['sku'],p['seller']):p for p in publications}
    qkeys={(q['owner'],q['sku'],q['seller']):q for q in queues}
    if len(pkeys)!=len(publications) or len(qkeys)!=len(queues) or pkeys.keys()!=qkeys.keys():return 'consumer_identity'
    if any(q['state']!='selling' or q.get('offer_id')!=pkeys[k]['offer_id'] for k,q in qkeys.items()):return 'pipeline_not_complete'
    # The publisher also writes a terminal jobs projection of the SAME intent.
    # Only that proven mirror is non-consuming; ordinary legacy jobs stay protected.
    for job in jobs:
        if not isinstance(job,dict) or job.get('phase')!='selling' or job.get('lease') or job.get('lease_until',0)>time.time():return 'other_consumer'
        record=job.get('external_publication',{})
        key=(record.get('owner'),record.get('sku'),record.get('seller'))
        publication=pkeys.get(key)
        if not publication or record.get('phase')!='stock_verified' or record.get('verified') is not True:return 'other_consumer'
        if any(record.get(k)!=publication.get(k) for k in ('owner','sku','seller','offer_id','store_id')):return 'other_consumer'
        if (job.get('id'),job.get('owner'),job.get('source_key'),job.get('store_id'))!=(publication['offer_id'],publication['owner'],publication['sku'],publication['store_id']):return 'other_consumer'
    return 'eligible_for_remote_proof'


def local_proof(db, item):
    """Use within source_guard; transactions short and no remote I/O inside."""
    sku=item['sku']
    with db.connect() as c:
        pubs=[json.loads(r[0]) for r in c.execute('SELECT body FROM plugin_publications WHERE sku=?',(sku,))]
        qs=[]
        for r in c.execute('SELECT owner,sku,seller,state,body FROM plugin_pipeline WHERE sku=?',(sku,)):
            q=dict(r);q['offer_id']=json.loads(q.pop('body')).get('offer_id');qs.append(q)
        row=c.execute('SELECT state,body,updated FROM source_details WHERE key=?',(item['source_key'],)).fetchone()
        if not row:return 'missing_source',None
        source=db.open(row['body'])|{'state':row['state']}
        if str(source.get('source_key'))!=sku:return 'source_identity',None
        exists=lambda t:bool(c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(t,)).fetchone())
        jobs=[]
        for r in c.execute('SELECT id,owner,source_key,store_id,phase,lease,lease_until,data FROM jobs WHERE source_key=?',(sku,)):
            job=dict(r);job['external_publication']=json.loads(job.pop('data')).get('external_publication',{});jobs.append(job)
        acq=c.execute('SELECT task_key FROM acquisition_bindings WHERE sku=?',(sku,)).fetchall() if exists('acquisition_bindings') else []
        leases=c.execute('SELECT token FROM plugin_pipeline_leases WHERE sku=? AND expires>?',(sku,time.time())).fetchall()
        drafts=c.execute('SELECT state FROM draft_cleanup_receipts WHERE draft_id=?',(str(source.get('draft_id')),)).fetchall() if exists('draft_cleanup_receipts') else []
        receipts=c.execute('SELECT state FROM favorite_cleanup_receipts WHERE favorite_id=?',(item['favorite_id'],)).fetchall() if exists('favorite_cleanup_receipts') else []
    reason=evaluate(pubs,qs,source=source,favorite_id=item['favorite_id'],jobs=jobs,acquisition=acq,leases=leases,draft_receipts=drafts,favorite_receipts=receipts)
    proof={'jobs':jobs,'publications':pubs,'queues':qs,'source':source,'source_updated':row['updated'],'observed':time.time()}
    return reason,proof


async def execute_one(db,item,client,contexts,archive_directory):
    """One bounded mutation, protected by the same SKU guard as consumers.

    contexts maps immutable store IDs to the verified account's clients/config.
    Caller never retries a deletion on missing acknowledgement.
    """
    import os
    from .favorite_cleanup import exact_favorites, rows
    with source_guard(db.directory,item['sku']):
        with db.connect() as c:
            schema(c)
            if c.execute('SELECT 1 FROM favorite_release_certificates WHERE account=? AND favorite_id=?',
                         (item['account'],item['favorite_id'])).fetchone():return {'state':'prior_intent_protected'}
        reason,proof=local_proof(db,item)
        if reason!='eligible_for_remote_proof':return {'state':'protected','reason':reason}
        _,favorites=await exact_favorites(client,item['sku'])
        if len(favorites)!=1 or str(favorites[0]['id'])!=item['favorite_id']:return {'state':'protected','reason':'remote_identity'}
        detail=await client.call('/api.product.collect/detail',params={'id':str(proof['source']['draft_id']),'is_online':0})
        if not isinstance(detail,dict) or not detail.get('skus'):return {'state':'protected','reason':'independent_draft_missing'}
        online=[]
        for p in proof['publications']:
            ctx=contexts.get(p['store_id'])
            if ctx is None:return {'state':'protected','reason':'account_store_identity'}
            reply=await ctx['client'].call('/api.product.online/lists',params={'page':1,'page_size':100,'shop_id':ctx['shop_id'],'offer_id':p['offer_id']})
            exact=[r for r in rows(reply) if str(r.get('shop_id'))==str(ctx['shop_id']) and str(r.get('offer_id'))==p['offer_id']]
            stock=float(exact[0].get('stock') or 0) if len(exact)==1 else 0
            if len(exact)!=1 or exact[0].get('online_status')!='selling' or not math.isfinite(stock) or stock<=0:
                return {'state':'protected','reason':'online_not_verified'}
            online.append(exact[0])
        reason,latest=local_proof(db,item)
        if reason!='eligible_for_remote_proof' or latest['source_updated']!=proof['source_updated']:
            return {'state':'protected','reason':'dependency_changed'}
        proof=latest|{'favorite':favorites[0],'fresh_independent_draft':detail,'online':online,
                     'release_basis':'all recorded SKU publications verified; no other consumer; retain independent draft for existing freshness/reprocessing path',
                     'business_revision':item['business_revision']}
        archive_directory=Path(archive_directory);archive_directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        encoded=json.dumps(proof,sort_keys=True).encode();digest=hashlib.sha256(encoded).hexdigest();path=archive_directory/(digest+'.json')
        with path.open('xb') as f:f.write(encoded);f.flush();os.fsync(f.fileno())
        directory_fd=os.open(archive_directory,os.O_RDONLY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
        proof['archive_path']=str(path);proof['archive_sha256']=digest
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM draft_cleanup_receipts WHERE draft_id=?',(str(proof['source']['draft_id']),)).fetchone():
                return {'state':'protected','reason':'draft_deletion_race'}
            c.execute('INSERT INTO favorite_release_certificates VALUES(?,?,?,?,?,?,?,?)',
                      (item['account'],item['favorite_id'],item['sku'],item['source_key'],str(proof['source']['draft_id']),'intent',db.seal(proof),time.time()))
        # Pin persists even after timeout/crash. Never drop it or replay this ID.
        favorite=favorites[0]
        state='acknowledged'
        try:
            proof['response']=await client.call('/api.product.favorite/toggle',method='POST',body={'status':False,'productInfo':favorite|{
                'sku':item['sku'],'coverImage':favorite.get('cover_image',''),'price_info':{'sell_price':favorite.get('sell_price'),'currency':'CNY'}}})
        except Exception as error:
            state='uncertain';proof['error_type']=type(error).__name__
        with db.connect() as c:
            c.execute('UPDATE favorite_release_certificates SET state=?,proof=?,updated=? WHERE account=? AND favorite_id=?',
                      (state,db.seal(proof),time.time(),item['account'],item['favorite_id']))
        # Lost acknowledgements allow reads only, and do not block the next SKU.
        try:
            _,present=await exact_favorites(client,item['sku'])
            after=await client.call('/api.product.collect/detail',params={'id':str(proof['source']['draft_id']),'is_online':0})
            if item['favorite_id'] not in {str(r['id']) for r in present} and isinstance(after,dict) and after.get('skus'):
                state='deleted';proof['absence_verified_at']=time.time();proof['retained_draft_verified_at']=time.time()
            elif not isinstance(after,dict) or not after.get('skus'):
                state='dependent_draft_check_failed'
        except Exception as error:proof['readback_error_type']=type(error).__name__
        with db.connect() as c:
            c.execute('UPDATE favorite_release_certificates SET state=?,proof=?,updated=? WHERE account=? AND favorite_id=?',
                      (state,db.seal(proof),time.time(),item['account'],item['favorite_id']))
        return {'state':state,'sku':item['sku'],'favorite_id':item['favorite_id'],'draft_id':str(proof['source']['draft_id']),'archive_sha256':digest}
