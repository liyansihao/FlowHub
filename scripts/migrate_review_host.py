#!/usr/bin/env python3
"""Migrate the review host without losing pending commands. No platform credentials are uploaded."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse

import httpx
from flowhub.db import Database
from flowhub.remote_reviews import current_snapshot, settings
from flowhub.review_transport import request as review_request


def save(path, text):
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w') as f:
        os.chmod(temp,0o600)
        f.write(text)
    temp.replace(path)


def target_config(db,url):
    path=db.directory/'cloudflare-review.env'
    values={}
    if path.exists():
        values=dict(line.split('=',1) for line in path.read_text().splitlines() if '=' in line)
    values.update(FLOWHUB_REVIEW_SYNC_URL=url,FLOWHUB_REVIEW_OWNER=settings(db)['FLOWHUB_REVIEW_OWNER'],FLOWHUB_REVIEW_SYNC_INTERVAL='30')
    values.setdefault('FLOWHUB_REVIEW_SYNC_TOKEN',secrets.token_urlsafe(40))
    save(path,'\n'.join(f'{k}={v}' for k,v in values.items())+'\n')
    return values,path


async def migrate(db,url,activate):
    original=settings(db)
    if original.get('FLOWHUB_REVIEW_SYNC_URL')==url:
        raise ValueError('Already using this host; inspect synchronization instead of repeating migration')
    target,path=target_config(db,url)
    if not activate:
        print(json.dumps({'ready':True,'private_config':str(path),'next':'Install REVIEW_SYNC_TOKEN from this private file as a Cloudflare Worker secret, then rerun with --activate.'}))
        return
    runtime=db.directory/'remote-review-runtime.json'
    if not runtime.exists() or json.loads(runtime.read_text()).get('version')!=2 or time.time()-json.loads(runtime.read_text()).get('at',0)>90:
        raise ValueError('Reload the local worker with migration locking support before activating')
    backup=db.directory/f'review-migration-{int(time.time())}'
    backup.mkdir(mode=0o700)
    save(backup/'old.env','\n'.join(f'{k}={v}' for k,v in original.items())+'\n')
    async with httpx.AsyncClient(timeout=60,trust_env=False) as client:
        async def request(config,mode='',body=None):
            response=await review_request(client,config,'POST' if body is not None else 'GET',params={'mode':mode} if mode else None,body=body)
            response.raise_for_status()
            return response.json()
        with (db.directory/'remote-review-sync.lock').open('a') as lock:
            # Bounded lock wait; a normal sync may still be finishing.
            for _ in range(90):
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:await asyncio.sleep(1)
            else:raise TimeoutError('Review sync is still busy; no changes made')
            old_snapshot=current_snapshot(db,original['FLOWHUB_REVIEW_OWNER'])
            old_history=await request(original)
            save(backup/'old-review.json',json.dumps(old_history,ensure_ascii=False))
            # Verify the destination accepts a complete snapshot BEFORE disabling the old site.
            await request(target,'refresh',old_snapshot)
            check=await review_request(client,target,'GET',params={'page':0,'page_size':12})
            check.raise_for_status()
            if check.json()['total']!=len(old_snapshot['items']):raise ValueError('Destination product count mismatch')
            disabled={**old_snapshot,'items':[{**x,'allowed_actions':[],'can_approve':False,'can_repair':False,'can_reject':False,'can_list':False,'can_unlist':False,'approval_block':'审核台已迁移，请使用新网址'} for x in old_snapshot['items']]}
            frozen=False
            try:
                await request(original,'refresh',disabled)
                frozen=True
                # Read after freeze so commands submitted just before the freeze are included.
                final=await request(original)
                save(backup/'frozen-review.json',json.dumps(final,ensure_ascii=False))
                events=[x for x in final.get('history',[]) if not str(x.get('id','')).startswith('local-')]
                pending=await request(original,'sync')
                events=list({x['id']:x for x in events+pending['items']}.values())
                for offset in range(0,len(events),500):
                    await request(target,'import-history',{'owner':original['FLOWHUB_REVIEW_OWNER'],'items':events[offset:offset+500]})
                migrated=await request(target,'sync')
                if not {x['id'] for x in pending['items']} <= {x['id'] for x in migrated['items']}:
                    raise ValueError('Pending command transfer incomplete')
                save(db.directory/'review-sync.env',path.read_text())
            except Exception:
                if frozen:await request(original,'refresh',old_snapshot)
                raise
    print(json.dumps({'activated':True,'url':url,'products':len(old_snapshot['items']),'pending_transferred':len(pending['items']),'remote_history':len(events),'backup':str(backup)},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    parsed=urlparse(args.url)
    if parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.workers.dev') or parsed.path!='/api/reviews':
        parser.error('Use the verified https://<worker>.<account>.workers.dev/api/reviews URL')
    asyncio.run(migrate(Database(),args.url,args.activate))

if __name__=='__main__':main()
