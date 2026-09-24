"""Read-only remote reconciliation of unresolved collection-box writes.

An absent or incomplete listing never proves a historical import failed. Only
an exact original-account/SKU draft plus its detail can confirm a reservation.
"""
import asyncio
import hashlib
import json
import time

from .collection_capacity import account
from .pipeline_modules.database_work import run as database_work
from .source_detail import SourceCollector


def _prepare(db, now, limit):
    with db.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS collection_reservation_rechecks(
            account TEXT, source_key TEXT, next_at REAL, result TEXT, checked_at REAL,
            PRIMARY KEY(account,source_key))''')
        reservations=c.execute('''SELECT r.account,r.source_key,s.state,s.body,s.updated
            FROM collection_reservations r
            LEFT JOIN collection_reservation_rechecks x USING(account,source_key)
            LEFT JOIN source_details s ON s.key=r.source_key
            WHERE r.state='unknown' AND COALESCE(x.next_at,0)<=?
            ORDER BY COALESCE(x.checked_at,0),r.updated LIMIT ?''',(now,limit)).fetchall()
        stores=c.execute('SELECT owner,config,secret FROM stores WHERE enabled=1').fetchall()
    contexts={}
    for row in stores:
        credentials=db.open(row['secret'])
        if not credentials.get('erp_token'):continue
        ctx={'owner':row['owner'],'store':{'config':json.loads(row['config']),'credentials':credentials}}
        contexts[account(ctx)]=ctx
    jobs=[]
    for row in reservations:
        ctx=contexts.get(row['account'])
        data=db.open(row['body']) if row['body'] else {}
        sku=str(data.get('source_key') or '')
        if ctx and row['state']=='draft_started' and sku.isdigit():
            token_hash=hashlib.sha256(ctx['store']['credentials']['erp_token'].encode()).hexdigest()
            key=hashlib.sha256(f"{ctx['owner']}:{token_hash}:{sku}".encode()).hexdigest()
            if key==row['source_key']:
                jobs.append((row['account'],row['source_key'],row['updated'],sku,data,
                             ctx|{'candidate':{'source_key':sku}}))
                continue
        jobs.append((row['account'],row['source_key'],row['updated'],None,None,None))
    return jobs


async def _complete_listing(collector):
    rows=[];seen=set();total=None
    for page in range(1,21):
        response=await collector.call('/api.product.collect/lists',query={'page':page,'page_size':100})
        if not isinstance(response,dict) or not isinstance(response.get('data'),list):
            raise ValueError('invalid collection listing')
        count=response.get('total')
        if isinstance(count,bool) or not str(count).isdigit():raise ValueError('collection total missing')
        if total is None:total=int(count)
        if total!=int(count):raise ValueError('collection total changed')
        for row in response['data']:
            if not isinstance(row,dict) or not str(row.get('id','')).isdigit() or int(row['id'])<=0:
                raise ValueError('invalid draft row')
            identifier=str(row['id'])
            if identifier in seen:raise ValueError('repeated draft row')
            seen.add(identifier);rows.append(row)
        if len(rows)==total:return rows
        if len(rows)>total or not response['data']:raise ValueError('incomplete collection listing')
    raise ValueError('collection listing page limit')


def _checkpoint(db, job, result, draft=None, detail=None):
    scope,key,updated,sku,data,_=job;now=time.time()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute("SELECT state,updated FROM source_details WHERE key=?",(key,)).fetchone()
        reservation=c.execute('SELECT state FROM collection_reservations WHERE account=? AND source_key=?',(scope,key)).fetchone()
        if result=='confirmed' and current and current['state']=='draft_started' and current['updated']==updated and reservation and reservation['state']=='unknown':
            completed=data|{'draft_id':int(draft),'detail':detail,'observed_at':now,
                'recovery':{'source':'independent-exact-account-draft-recheck','at':now}}
            c.execute("UPDATE source_details SET state='ready',body=?,updated=? WHERE key=? AND state='draft_started' AND updated=?",
                      (db.seal(completed),now,key,updated))
            c.execute("UPDATE collection_reservations SET state='confirmed',updated=? WHERE account=? AND source_key=? AND state='unknown'",
                      (now,scope,key))
        else:result='source_changed' if result=='confirmed' else result
        delay=3600 if result in ('not_found','unmatched_source') else 300
        c.execute('INSERT OR REPLACE INTO collection_reservation_rechecks VALUES(?,?,?,?,?)',
                  (scope,key,now+delay,result,now))
    return result


async def recheck(db, *, limit=2):
    jobs=await database_work(_prepare,db,time.time(),limit)
    summary={'checked':0,'confirmed':0,'unresolved':0,'errors':0}
    listings={}
    for job in jobs:
        scope,key,_,sku,data,ctx=job
        if ctx is None:
            await database_work(_checkpoint,db,job,'unmatched_source')
            summary['unresolved']+=1;continue
        collector=await database_work(SourceCollector,db,ctx)
        try:
            if scope not in listings:listings[scope]=await _complete_listing(collector)
            matches=[r for r in listings[scope] if str(r.get('goods_id'))==sku and r.get('collect_from')=='ozon']
            if not matches:
                result='not_found';draft=detail=None
            elif len(matches)!=1:
                raise ValueError('ambiguous original SKU drafts')
            else:
                draft=str(matches[0]['id'])
                detail=await collector.call('/api.product.collect/detail',query={'id':int(draft),'is_online':0})
                if not isinstance(detail,dict) or not isinstance(detail.get('skus'),list) or not detail['skus']:
                    raise ValueError('incomplete exact draft detail')
                result='confirmed'
            result=await database_work(_checkpoint,db,job,result,draft,detail)
            summary['confirmed' if result=='confirmed' else 'unresolved']+=1
        except Exception:
            await database_work(_checkpoint,db,job,'read_error')
            summary['errors']+=1
        summary['checked']+=1
    return summary


async def run(db):
    while True:
        await recheck(db)
        await asyncio.sleep(60)
