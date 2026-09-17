"""Recover audited, unsubmitted official tasks only. Default is a read-only plan."""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.pipeline_modules.repair_queue import listing_authorized
from flowhub.pipeline_modules.repair_recovery import restored_identity


def recover(db, *, apply=False, limit=5):
    counts=Counter();tasks=[]
    with db.connect() as c:
        if apply:c.execute('BEGIN IMMEDIATE')
        rows=c.execute("""SELECT q.*,r.body review,p.body product FROM plugin_pipeline q
            JOIN plugin_reviews r USING(owner,sku,seller) JOIN sourcing_products p USING(owner,sku,seller)
            LEFT JOIN plugin_publications pub USING(owner,sku,seller)
            LEFT JOIN plugin_pipeline_leases l USING(owner,sku,seller)
            WHERE pub.body IS NULL AND (l.expires IS NULL OR l.expires<=?)
            AND q.state IN ('needs_review','same_product_confirmed','needs_fields')
            AND json_extract(q.body,'$.backend')='official'
            ORDER BY CASE WHEN json_extract(q.body,'$.error') LIKE '%400 Bad Request%attribute/values/search%' THEN 0 ELSE 1 END,q.due""",(time.time(),)).fetchall()
        for row in rows:
            if len(tasks)>=limit:break
            key=(row['owner'],row['sku'],row['seller']);q=json.loads(row['body']);review=json.loads(row['review'])
            if q.get('submitted') or q.get('offer_id'):continue
            if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',key[:2]).fetchone():continue
            if c.execute('SELECT 1 FROM jobs WHERE source_key=?',(key[1],)).fetchone():continue
            if not listing_authorized(c,key,q):continue
            control=c.execute('SELECT * FROM product_listing_controls WHERE owner=? AND sku=? AND seller=?',key).fetchone()
            if control['state']=='running':continue
            control_body=json.loads(control['body'])
            error=q.get('error','')
            bad_dictionary='400 Bad Request' in error and 'attribute/values/search' in error
            expired_recovery=(review.get('state')=='matched' and time.time()-review.get('finished_at',time.time())>=21600
                and (bad_dictionary or (q.get('repair_recovery') or {}).get('reason')=='single_character_dictionary_validation'))
            if row['state']=='needs_fields' and not expired_recovery:continue
            if bad_dictionary and not expired_recovery:
                reason='single_character_dictionary_validation';new_review=None;state='needs_fields'
                q.update(official_dossier_pending=True,repair_full_dossier=True,needs_dossier=True,phase='awaiting_dossier')
                for field in ('error','reason','repair_retry'):q.pop(field,None)
            elif expired_recovery or (q.get('repair_reason')=='complete_dossier' and 'result' not in review):
                receipt=c.execute('SELECT * FROM human_reviews WHERE owner=? AND sku=? AND seller=? ORDER BY at DESC,id DESC LIMIT 1',key).fetchone()
                if not receipt:continue
                try:new_review=restored_identity(receipt,json.loads(row['product']),review)
                except (ValueError,KeyError,TypeError) as error:
                    counts['retained:'+str(error)[:100]]+=1;continue
                reason='revalue_expired_approval' if expired_recovery else 'restore_original_human_approval'
                state='same_product_confirmed'
                q.update(same_product_only=True,reason='original_human_approval_restored')
                q.pop('error',None)
                for field in ('phase','error','next_attempt_at'):control_body.pop(field,None)
            else:continue
            tasks.append({'sku':key[1],'reason':reason});counts[reason]+=1
            if not apply:continue
            c.execute('''CREATE TABLE IF NOT EXISTS repair_recovery_receipts(
                id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,at REAL,reason TEXT,body TEXT)''')
            backup={'queue':dict(row),'control':dict(control)}
            receipt_id=c.execute('INSERT INTO repair_recovery_receipts VALUES(NULL,?,?,?,?,?,?)',
                                (*key,time.time(),reason,db.seal(backup))).lastrowid
            q['repair_recovery']={'receipt_id':receipt_id,'at':time.time(),'reason':reason}
            q['updated_at']=time.time()
            c.execute('UPDATE plugin_pipeline SET state=?,body=?,due=0,attempts=0 WHERE owner=? AND sku=? AND seller=?',
                      (state,json.dumps(q),*key))
            if new_review:
                c.execute('UPDATE plugin_reviews SET state=?,body=?,updated=? WHERE owner=? AND sku=? AND seller=?',
                          (new_review['state'],json.dumps(new_review),time.time(),*key))
                c.execute("UPDATE product_listing_controls SET state='queued',body=?,updated=? WHERE owner=? AND sku=? AND seller=?",
                          (json.dumps(control_body),time.time(),*key))
    return {'applied':apply,'counts':dict(counts),'tasks':tasks}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true');p.add_argument('--limit',type=int,default=5)
    args=p.parse_args()
    if not 1<=args.limit<=100:raise SystemExit('limit must be 1..100')
    print(json.dumps(recover(Database(),apply=args.apply,limit=args.limit),ensure_ascii=False))
