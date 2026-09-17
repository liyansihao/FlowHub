"""Reclassify pending identity reviews using existing bound evidence; no listing writes."""
import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.identity_review import envelope,bound,comparison,identity_result,review_state
from flowhub.manual_reviews import publication_started


def migrate(db,*,apply=False,backup_path=None):
    stats=collections.Counter();changes=[]
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute("SELECT q.*,p.body product,r.body review,r.state review_state,r.updated review_updated FROM plugin_pipeline q JOIN sourcing_products p USING(owner,sku,seller) LEFT JOIN plugin_reviews r USING(owner,sku,seller) WHERE q.state='needs_review'").fetchall()
        for row in rows:
            key=(row['owner'],row['sku'],row['seller']);q=json.loads(row['body']);r=json.loads(row['review'] or '{}')
            identity=r.get('identity_review') or {}
            if q.get('human_review') or identity.get('human_review') or q.get('force_identity_review'):
                stats['preserved_manual_or_forced']+=1;continue
            if publication_started(c,*key,q):stats['publication_skipped']+=1;continue
            if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',key[:2]).fetchone():stats['blocked_skipped']+=1;continue
            if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(*key,time.time())).fetchone():stats['active_skipped']+=1;continue
            if c.execute("SELECT 1 FROM product_listing_controls WHERE owner=? AND sku=? AND seller=? AND state IN ('queued','running','waiting','hold_queued','hold_waiting')",key).fetchone():stats['active_skipped']+=1;continue
            try:candidate=envelope(json.loads(row['product']))
            except ValueError:stats['missing_input']+=1;continue
            cb=identity.get('comparison') or comparison(r)
            if not bound(cb,candidate):stats['changed_input_skipped']+=1;continue
            result=identity_result(cb,candidate,reused=True,observed_at=r.get('finished_at'))
            state=review_state(result)
            # Do not launch expensive re-searches or publication from a data migration.
            # Unreviewed intermediate/high unknown-size pairs go through the review lane.
            if state=='needs_review' and not (cb.get('decision') or {}).get('qwen_review') and (cb.get('search_and_rank') or {}).get('candidates'):
                state='queued'
            if state=='needs_review':stats['manual_review_retained']+=1;continue
            changes.append({'before':dict(row),'after_state':state})
            stats[state]+=1
            if apply:
                q.update(same_product_only=True,reason='same_product_'+result['verdict'])
                q.pop('error',None)
                if state!='queued':
                    r.update(identity_review=result,state=state)
                    c.execute('UPDATE plugin_reviews SET state=?,body=?,updated=? WHERE owner=? AND sku=? AND seller=?',(state,json.dumps(r),time.time(),*key))
                c.execute('UPDATE plugin_pipeline SET state=?,body=?,due=0,attempts=0 WHERE owner=? AND sku=? AND seller=?',(state,json.dumps(q),*key))
        if apply and changes:
            if backup_path is None:raise ValueError('backup_path required')
            backup_path=Path(backup_path);backup_path.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(backup_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:json.dump(changes,f,ensure_ascii=False)
    return dict(stats)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');p.add_argument('--backup',type=Path)
    args=p.parse_args();print(json.dumps(migrate(Database(),apply=args.apply,backup_path=args.backup),ensure_ascii=False))
