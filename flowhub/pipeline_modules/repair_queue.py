"""Bound repair concurrency using repair outcomes, independently of legacy ERP publishing."""
import json
import time


def can_expand(db, now=None):
    now=time.time() if now is None else now
    with db.connect() as c:
        rows=c.execute("""SELECT outcome,details,finished-started duration FROM pipeline_module_events
            WHERE module='seed' AND json_extract(details,'$.lane')='seed_repair' AND finished>?
            ORDER BY finished DESC LIMIT 3""",(now-300,)).fetchall()
    # Successful official repairs must be able to enable the second worker even
    # when the old publication transport has no traffic at all.
    return len(rows)>=2 and all(r['outcome'] in ('queued','publishing','needs_review')
        and not json.loads(r['details']).get('reason') and 0<=r['duration']<120 for r in rows)


def listing_authorized(c,key,queue,review=None):
    identity=queue.get('listing_control_id')
    if not identity or not c.execute("SELECT 1 FROM sqlite_master WHERE name='product_listing_controls'").fetchone():
        return False
    row=c.execute('SELECT action,body FROM product_listing_controls WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    if not row or row['action']!='list' or json.loads(row['body']).get('id')!=identity:
        return False
    if review is not None:
        return review.get('state')=='matched' and (review.get('website_listing_authorization') or {}).get('id')==identity
    return True


def preserve_review(c,db,key,queue):
    row=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    if not row:return
    review=json.loads(row[0])
    c.execute('''CREATE TABLE IF NOT EXISTS repair_review_history(
        id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,at REAL,body TEXT)''')
    revision=c.execute('INSERT INTO repair_review_history VALUES(NULL,?,?,?,?,?)',
                       (*key,time.time(),db.seal(review))).lastrowid
    queue['repair_review_revision']=revision
    if listing_authorized(c,key,queue,review):
        queue['same_product_only']=False
    if not queue.get('same_product_only'):
        queue['force_full_evaluation']=True


async def tick_repair(db,index,tick):
    preferred='publication' if index==0 else 'valuation'
    if await tick(db,lane='seed_repair',repair_kind=preferred):return True
    return await tick(db,lane='seed_repair',repair_kind='valuation' if index==0 else 'publication')
