"""Versioned, acknowledged deltas for the hosted review replica."""
import hashlib
import json
import os
import time


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def save(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,separators=(',',':'))
    temporary.replace(path)


def read(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def prepare(snapshot,config,previous,remote_cursor):
    identity={'endpoint':config['FLOWHUB_REVIEW_SYNC_URL'],'owner':snapshot['owner']}
    valid=all(previous.get(k)==v for k,v in identity.items()) and previous.get('cursor')==remote_cursor
    item_hashes={json.dumps([x['sku'],x['seller']],separators=(',',':')):digest(x) for x in snapshot['items']}
    history_hashes={x['id']:digest(x) for x in snapshot.get('local_history',[])}
    old_items=previous.get('items',{}) if valid else {}
    old_history=previous.get('history',{}) if valid else {}
    # History is append-only, including records no longer in the recent local window.
    history_hashes=old_history | history_hashes
    checkpoint={**identity,'items':item_hashes,'history':history_hashes}
    checkpoint['cursor']=digest(checkpoint)
    if valid and checkpoint['cursor']==previous.get('cursor'):return None,None,checkpoint
    if not valid:return 'refresh',{**snapshot,'cursor':checkpoint['cursor']},checkpoint
    changed=[x for x in snapshot['items'] if old_items.get(json.dumps([x['sku'],x['seller']],separators=(',',':')))!=item_hashes[json.dumps([x['sku'],x['seller']],separators=(',',':'))]]
    removed=[dict(zip(('sku','seller'),json.loads(k))) for k in old_items.keys()-item_hashes.keys()]
    history=[x for x in snapshot.get('local_history',[]) if old_history.get(x['id'])!=history_hashes[x['id']]]
    return 'delta',{'owner':snapshot['owner'],'base_cursor':remote_cursor,'cursor':checkpoint['cursor'],'generated_at':snapshot['generated_at'],'upserts':changed,'removed':removed,'history':history},checkpoint


async def upload(db,config,client,snapshot,remote_cursor,request):
    path=db.directory/'review-delta-checkpoint.json'
    mode,body,checkpoint=prepare(snapshot,config,read(path),remote_cursor)
    metrics_path=db.directory/'review-delta-metrics.json';metrics=read(metrics_path)
    if mode:
        response=await request(client,config,'POST',params={'mode':mode},body=body)
        response.raise_for_status()
        if response.json().get('cursor')!=checkpoint['cursor']:raise ValueError('remote review cursor not acknowledged')
        # Never advance the local checkpoint before an exact server acknowledgement.
        save(path,checkpoint)
        metrics[mode+'_uploads']=metrics.get(mode+'_uploads',0)+1
        size=len(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode())
        metrics['uploaded_json_bytes']=metrics.get('uploaded_json_bytes',0)+size
        metrics['last_upload_json_bytes']=size
    else:
        metrics['skipped_uploads']=metrics.get('skipped_uploads',0)+1
        metrics['last_upload_json_bytes']=0
    metrics.update(at=time.time(),last_mode=mode or 'unchanged',last_products=len(snapshot['items']),last_changed=len(body.get('upserts',body.get('items',[]))) if body else 0)
    save(metrics_path,metrics)


def next_delay(previous,base,maximum,viewer_active,pending):
    if viewer_active or pending:return base
    return min(maximum,max(base,previous*2))
