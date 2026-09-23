"""Explicit bounded favorite batches owned by the existing supervised worker.

No extra SQLite writer process. Request/progress files contain identities only;
all deletion evidence remains in the existing certificates and private archive.
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import time
import traceback
from urllib.parse import urlsplit
from pathlib import Path
from .database_work import read
from .favorite_cleanup import Client
from .favorite_release import execute_one
from ..modules import Pending


def persist(path, value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)


def validate_proxy(value):
    if value is None:return None
    parsed=urlsplit(value)
    if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','::1') or not parsed.port
        or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('only an explicit local proxy may be selected for this batch')
    return value


def context(db,item,loopback_proxy=None):
    with db.connect() as c:
        pubs=[json.loads(r[0]) for r in c.execute('SELECT body FROM plugin_publications WHERE sku=?',(item['sku'],))]
        stores={r['id']:dict(r) for r in c.execute('SELECT * FROM stores')}
    if not pubs:return None
    store=stores.get(pubs[0].get('store_id'))
    if not store:return None
    token=db.open(store['secret']).get('erp_token');owner=pubs[0]['owner']
    if not token:return None
    expected=hashlib.sha256((owner+':'+hashlib.sha256(token.encode()).hexdigest()+':'+item['sku']).encode()).hexdigest()
    if expected!=item['source_key']:return None
    contexts={}
    for p in pubs:
        st=stores.get(p.get('store_id'))
        if not st:return None
        keys=db.open(st['secret'])
        if p['owner']!=owner or keys.get('erp_token')!=token:return None
        cfg=json.loads(st['config'])
        if loopback_proxy is not None:cfg['erp_proxy']=validate_proxy(loopback_proxy)
        contexts[p['store_id']]={'client':Client({'store':{'config':cfg,'credentials':keys}}),'shop_id':cfg['shop_id']}
    return item|{'account':hashlib.sha256((owner+':'+token).encode()).hexdigest()},contexts[pubs[0]['store_id']]['client'],contexts


def error_details(error):
    detail={'type':type(error).__name__,'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)]}
    if isinstance(error,sqlite3.Error):detail.update(sqlite_code=getattr(error,'sqlite_errorcode',None),sqlite_name=getattr(error,'sqlite_errorname',None),sqlite_message=str(error))
    return detail


async def tick(db):
    request_path=db.directory/'favorite-release-request.json'
    if not request_path.exists():return False
    request=json.loads(request_path.read_text())
    if not request.get('enabled'):return False
    proxy=validate_proxy(request.get('loopback_proxy'))
    batch=request['batch_id']
    if not batch or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for ch in batch):raise ValueError('invalid batch id')
    if not 1<=request['max_deletes']<=1000 or not 1<=len(request['items'])<=1000 or not 1<=request['seconds']<=1800:raise ValueError('invalid batch bounds')
    runtime=json.loads((db.directory/'runtime-worker.json').read_text())['identity']
    if runtime['pid']!=os.getpid() or runtime['source_revision']!=request['revision']:raise ValueError('batch revision/worker mismatch')
    status_path=db.directory/f'favorite-release-batch-{batch}.json'
    state=json.loads(status_path.read_text()) if status_path.exists() else {'batch_id':batch,'revision':request['revision'],'started':time.time(),'records':[],'state':'running'}
    if state['state']!='running':return False
    client=None
    for item in request['items'][len(state['records']):]:
        if sum(r['result']['state']=='deleted' for r in state['records'])>=request['max_deletes'] or time.time()-state['started']>=request['seconds']:break
        fresh=json.loads(request_path.read_text())
        if not fresh.get('enabled') or fresh['batch_id']!=batch:return False
        started=time.time();client=None
        try:
            prepared=await read(context,db,item,proxy)
            if prepared is None:result={'state':'protected','reason':'account_identity'}
            else:
                item,client,contexts=prepared
                async with asyncio.timeout(90):
                    if 'before' not in state:
                        header=await client.call('/api.product.favorite/lists',params={'page':1,'page_size':1})
                        state['before']={k:header.get(k) for k in ('total','used','limit')}
                    result=await execute_one(db,item|{'business_revision':request['revision']},client,contexts,db.directory/'favorite-release-archives')
        except Pending:result={'state':'protected','reason':'active_consumer'}
        except asyncio.CancelledError:raise
        except Exception as error:
            result={'state':'error','error':error_details(error)}
            if isinstance(error,sqlite3.Error):state['state']='stopped_database_error'
        state['records'].append({'sku':item['sku'],'favorite_id':item['favorite_id'],'seconds':time.time()-started,'result':result})
        state['updated']=time.time()
        if result['state'] in ('dependent_draft_check_failed','uncertain','acknowledged'):state['state']='stopped_unconfirmed_delete'
        await read(persist,status_path,state)
        if state['state']!='running':break
        # Network work never holds the DB queue. Other supervised lanes continue.
        await asyncio.sleep(0)
    if state['state']=='running':state['state']='finished'
    state['finished']=time.time()
    if client:
        try:
            header=await client.call('/api.product.favorite/lists',params={'page':1,'page_size':1})
            state['after']={k:header.get(k) for k in ('total','used','limit')}
        except Exception as error:state['after_error']=error_details(error)
    await read(persist,status_path,state)
    return True


async def run(db):
    while True:
        await tick(db)
        await asyncio.sleep(5)
