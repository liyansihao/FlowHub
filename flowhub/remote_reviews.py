"""Pull decisions from the hosted review queue into the local FlowHub writer."""
import asyncio
import fcntl
import json
import os
import time

import httpx
from .review_transport import request as review_request


def current_snapshot(db, owner):
    from .manual_reviews import card, load, publication_started
    with db.connect() as c:
        rows = c.execute("""SELECT sku,seller FROM plugin_pipeline
            WHERE owner=? ORDER BY sku,seller""", (owner,)).fetchall()
        import json
        items=[]
        for row in rows:
            record=load(c,owner,row['sku'],row['seller'])
            from .listing_controls import management_card
            items.append(management_card(c,owner,record,row['sku'],row['seller']))
        from .sellable_audit_policy import cards as audit_cards
        items.extend(audit_cards(c,owner))
        history=[]
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='human_reviews'").fetchone():
            for event in c.execute('SELECT id,sku,seller,actor,action,note,revision,at,body FROM human_reviews WHERE owner=? ORDER BY id DESC LIMIT 1000',(owner,)):
                if event['action'] not in ('list','unlist') and not json.loads(json.loads(event['body']).get('queue','{}')).get('same_product_only'):continue
                history.append({'id':'local-'+str(event['id']),'sku':event['sku'],'seller':event['seller'],
                    'reviewer':event['actor'],'action':event['action'],'note':event['note'],'revision':event['revision'],
                    'status':'applied','created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(event['at']))})
    return {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'owner': owner, 'total': len(items), 'items': items,'local_history':history}


def settings(db):
    values = {key: value for key, value in os.environ.items() if key.startswith("FLOWHUB_REVIEW_")}
    path = db.directory / "review-sync.env"
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.startswith("FLOWHUB_REVIEW_") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip()
    return values


async def once(db, *, refresh=True):
    # Migration holds this same lock across freeze, pending-command transfer and URL switch.
    with (db.directory / "remote-review-sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        (db.directory / "remote-review-runtime.json").write_text(json.dumps({"version": 2, "at": time.time()}))
        return await sync_once(db, settings(db), refresh=refresh)


async def sync_once(db, config, *, refresh=True):
    endpoint = config.get("FLOWHUB_REVIEW_SYNC_URL", "").rstrip("/")
    token = config.get("FLOWHUB_REVIEW_SYNC_TOKEN", "")
    owner = config.get("FLOWHUB_REVIEW_OWNER", "")
    if not endpoint or not token or not owner:
        return 0
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        r = await review_request(client, config, 'GET', params={'mode':'sync'})
        r.raise_for_status()
        remote = r.json()
        decisions = remote.get("items", [])
        from .review_delta import save
        save(db.directory/'review-poll-state.json',{'at':time.time(),'viewer_active':bool(remote.get('viewer_active')),'pending':len(decisions)})
        applied = 0
        for item in decisions:
            try:
                if item.get('owner') != owner:
                    raise ValueError('审核租户不匹配，请刷新当前租户快照')
                from .manual_reviews import decide
                result = decide(db, owner, item["reviewer"], item["sku"], item["seller"], item["action"], item["note"], item["revision"], replay=True, review_scope="same_product_only" if item["action"] in ("approve","reject") else None)
                status, error = ("applied", "") if result else ("error", "本地审核未返回结果")
                applied += status == "applied"
            except ValueError as exc:
                status, error = "rejected", str(exc)
            except Exception as exc:  # transient failures remain pending
                status, error = "error", str(exc)
            try:
                ack = await review_request(client, config, 'POST', params={'mode':'ack'}, body={'id':item['id'],'status':status,'error':error})
                ack.raise_for_status()
            except Exception:
                db.health("remote-review-ack-error")
        if decisions or refresh:
            snapshot=current_snapshot(db, owner)
            if config.get('FLOWHUB_REVIEW_PROTOCOL')=='delta-v1' and remote.get('protocol')=='delta-v1':
                from .review_delta import upload
                await upload(db,config,client,snapshot,remote.get('cursor',''),review_request)
            else:
                response = await review_request(client, config, 'POST', params={'mode':'refresh'}, body=snapshot)
                response.raise_for_status()
        return applied


async def run(db):
    from .review_delta import next_delay,read
    delay=30
    while True:
        config=settings(db)
        base=max(5,float(config.get('FLOWHUB_REVIEW_SYNC_INTERVAL','30')))
        maximum=max(base,min(600,float(config.get('FLOWHUB_REVIEW_IDLE_MAX_SECONDS',str(base)))))
        try:
            await once(db,refresh=True)
            state=read(db.directory/'review-poll-state.json')
            delay=next_delay(delay,base,maximum,state.get('viewer_active',False),state.get('pending',0)>0)
        except Exception:
            db.health("remote-review-sync-error")
            delay=min(maximum,max(base,delay*2))
        await asyncio.sleep(delay)
