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
    if mode and remote_cursor and (mode=='refresh' or len(json.dumps(body).encode())>1_000_000):
        # Cursor recovery uploads bounded idempotent deltas. Preserve remote rows
        # unknown to the prior local checkpoint; never infer their deletion.
        old=read(path)
        removals=body.get('removed',[]) if mode=='delta' else [dict(zip(('sku','seller'),json.loads(k))) for k in old.get('items',{}) if k not in checkpoint['items']]
        items=body.get('upserts',body.get('items',[]));history=body.get('history',body.get('local_history',[]))
        batches=[]
        for field,values in [('upserts',items),('removed',removals),('history',history)]:
            batch=[];size=0
            for value in values:
                n=len(json.dumps(value,ensure_ascii=False).encode())
                if batch and (size+n>500_000 or len(batch)>=100):batches.append((field,batch));batch=[];size=0
                batch.append(value);size+=n
            if batch:batches.append((field,batch))
        if not batches:batches=[('upserts',[])]
        cursor=remote_cursor;total_bytes=0
        for index,(field,values) in enumerate(batches):
            next_cursor=checkpoint['cursor'] if index==len(batches)-1 else digest([checkpoint['cursor'],cursor,index,values])
            chunk={'owner':snapshot['owner'],'base_cursor':cursor,'cursor':next_cursor,'generated_at':snapshot['generated_at'],'upserts':[],'removed':[],'history':[],field:values}
            response=await request(client,config,'POST',params={'mode':'delta'},body=chunk)
            response.raise_for_status()
            if response.json().get('cursor')!=next_cursor:raise ValueError('chunk cursor not acknowledged')
            cursor=next_cursor;total_bytes+=len(json.dumps(chunk).encode())
        save(path,checkpoint)
        metrics.update(at=time.time(),last_mode='chunked_delta',last_products=len(snapshot['items']),last_changed=len(items),last_upload_json_bytes=total_bytes)
        metrics['delta_uploads']=metrics.get('delta_uploads',0)+len(batches)
        metrics['uploaded_json_bytes']=metrics.get('uploaded_json_bytes',0)+total_bytes
        save(metrics_path,metrics)
        return
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
