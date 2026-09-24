"""Threshold-triggered archived draft reclamation, invoked by a login timer."""
import argparse
import asyncio
import fcntl
import json
import os
import sqlite3
import time
from pathlib import Path

from .collection_capacity import account, observe
from .pipeline_modules.draft_cleanup import Client
from .db import Database


def atomic(path, value):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    tmp.chmod(0o600);tmp.replace(path)


async def tick(db, *, client_factory=Client):
    policy_path=db.directory/'collection-autoclean.json'
    policy=json.loads(policy_path.read_text()) if policy_path.exists() else {}
    if not policy.get('enabled'):return {'state':'disabled'}
    threshold=policy.get('threshold_ratio',.85)
    if not 0<threshold<1 or policy.get('target_ratio',0)!=0:raise ValueError('invalid collection autoclean thresholds')
    with (db.directory/'collection-autoclean.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'busy'}
        path=db.directory/'collection-autoclean-status.json'
        previous=json.loads(path.read_text()) if path.exists() else {}
        if previous.get('retry_at',0)>time.time():return previous
        with db.connect() as c:
            if c.execute('SELECT 1 FROM pipeline_module_control WHERE paused=1').fetchone():
                return {'state':'paused'}
            stores=c.execute('''SELECT s.* FROM stores s JOIN pipeline_campaigns p ON p.owner=s.owner
                JOIN users u ON u.id=s.owner WHERE s.enabled=1 AND p.enabled=1 AND u.active=1''').fetchall()
        contexts={}
        for row in stores:
            ctx={'owner':row['owner'],'store':{'config':json.loads(row['config']),'credentials':db.open(row['secret'])}}
            if ctx['store']['credentials'].get('erp_token'):contexts[account(ctx)]=ctx
        if not contexts:return {'state':'inactive'}
        if len(contexts)!=1:raise ValueError('automatic collection reclamation requires a single account')
        scope,ctx=next(iter(contexts.items()))
        status={'checked_at':time.time(),'pid':os.getpid(),'scope':scope,'threshold_ratio':threshold,'target_ratio':0}
        try:
            header=await client_factory(ctx).call('/api.product.collect/lists',params={'page':1,'page_size':1})
            used,limit=header.get('used'),header.get('limit')
            if any(isinstance(v,bool) or not str(v).isdigit() for v in (used,limit)) or int(limit)<=0:
                raise ValueError('invalid capacity observation')
            used,limit=int(used),int(limit)
            status.update(used=used,limit=limit)
            for attempt in range(3):
                try:
                    await asyncio.to_thread(observe,db,scope,header,status['checked_at'])
                    break
                except sqlite3.OperationalError as error:
                    if 'locked' not in str(error).lower() or attempt==2:raise
                    await asyncio.sleep(1)
            if used<limit*threshold:
                status['state']='below_threshold'
            else:
                # The old maintenance emptied the whole box and drained all
                # publishing lanes. The bounded, completed-only cleanup runs
                # inside the sole worker and keeps per-draft safety checks.
                worker_policy=db.directory/'draft-cleanup.json'
                worker=json.loads(worker_policy.read_text()) if worker_policy.exists() else {}
                status['state']='worker_cleanup_active' if worker.get('enabled') else 'worker_cleanup_disabled'
        except Exception as error:
            status.update(state='error',error_type=type(error).__name__,retry_at=time.time()+1800)
        status['finished_at']=time.time();atomic(path,status)
        with (db.directory/'collection-autoclean-events.jsonl').open('a') as output:
            if status['state']!='below_threshold' or previous.get('state')!='below_threshold':
                output.write(json.dumps(status,ensure_ascii=False)+'\n')
        return status


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data',type=Path,required=True)
    args=parser.parse_args()
    db=Database.open_existing(args.data)
    try:result=asyncio.run(tick(db))
    except Exception as error:
        result={'state':'error','error_type':type(error).__name__,'checked_at':time.time(),'retry_at':time.time()+1800}
        atomic(db.directory/'collection-autoclean-status.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='maintenance'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
