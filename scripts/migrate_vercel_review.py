"""Stage the local review snapshot and applied history on Vercel; activate separately."""
import argparse,asyncio,fcntl,json,os,time
from pathlib import Path
import httpx
from flowhub.db import Database
from flowhub.remote_reviews import current_snapshot
from flowhub.review_transport import request

def private_write(path,value):
    tmp=path.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(value)
    tmp.replace(path)

async def main(activate):
    db=Database();p=db.directory/'vercel-review.env'
    cfg=dict(line.split('=',1) for line in p.read_text().splitlines() if '=' in line)
    backup=db.directory/'vercel-migration-20260916';backup.mkdir(exist_ok=True,mode=0o700)
    if not (backup/'old.env').exists():private_write(backup/'old.env',(db.directory/'review-sync.env').read_text())
    async with httpx.AsyncClient(timeout=60,trust_env=False) as client:
        async def call(method,params,body=None):
            r=await request(client,cfg,method,params=params,body=body)
            if r.status_code>=400:raise RuntimeError(f'Vercel status {r.status_code}: {r.text[:200]}')
            return r.json()
        snapshot=current_snapshot(db,cfg['FLOWHUB_REVIEW_OWNER'])
        private_write(backup/'local-snapshot.json',json.dumps(snapshot,ensure_ascii=False))
        refreshed=await call('POST',{'mode':'refresh'},snapshot)
        history=[]
        with db.connect() as c:
            for e in c.execute('SELECT id,sku,seller,actor,action,note,revision,at,body FROM human_reviews WHERE owner=? ORDER BY id',(cfg['FLOWHUB_REVIEW_OWNER'],)):
                q=json.loads(json.loads(e['body']).get('queue','{}'))
                if e['action'] not in ('list','unlist') and not q.get('same_product_only'):continue
                history.append({'id':'local-'+str(e['id']),'owner':cfg['FLOWHUB_REVIEW_OWNER'],'sku':e['sku'],'seller':e['seller'],'action':e['action'],'note':e['note'],'reviewer':e['actor'],'revision':e['revision'],'status':'applied','created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(e['at']))})
        for i in range(0,len(history),400):await call('POST',{'mode':'import-history'},{'owner':cfg['FLOWHUB_REVIEW_OWNER'],'items':history[i:i+400]})
        check=await call('GET',{'view':'queue','page':0,'page_size':48})
        assert check['total']==len(snapshot['items'])
        assert all(x['pipeline_state']=='needs_review' for x in check['items'])
        if activate:
            with (db.directory/'remote-review-sync.lock').open('a') as lock:
                deadline=time.monotonic()+90
                while True:
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if time.monotonic()>deadline:raise TimeoutError('sync lock busy; config unchanged')
                        await asyncio.sleep(.5)
                private_write(db.directory/'review-sync.env',p.read_text())
        report={'activated':activate,'url':cfg['FLOWHUB_REVIEW_SYNC_URL'],'products':check['total'],'pending_review':check['queue_total'],'local_history_imported':len(history),'cloudflare_unread_commands':'preserved_in_old_database_not_transferred','at':time.time()}
        private_write(backup/'report.json',json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--activate',action='store_true');asyncio.run(main(p.parse_args().activate))
