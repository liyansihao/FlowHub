"""Park stalled local repair tasks; never touch remote favorites, drafts or stock."""
import json
import time


def cleanup(db, *, now=None):
    from . import isolation
    if isolation.enabled(db):
        return isolation.cleanup(db, now=now)
    from .admission import schema
    schema(db)
    now=time.time() if now is None else now
    parked=[]
    with db.write_transaction() as c:
        if c.execute("SELECT 1 FROM pipeline_module_control WHERE module='seed' AND paused=1").fetchone():
            return {'state':'paused','parked':0}
        c.execute('''CREATE TABLE IF NOT EXISTS repair_cleanup_receipts(
            id INTEGER PRIMARY KEY, owner TEXT, sku TEXT, seller TEXT, at REAL,
            reason TEXT, previous_state TEXT, previous_body TEXT, previous_due REAL, previous_attempts INTEGER)''')
        owners=c.execute('SELECT owner FROM pipeline_campaigns WHERE enabled=1').fetchall()
        for owner, in owners:
            rows=c.execute('''SELECT q.*,l.expires lease_expires FROM plugin_pipeline q
                LEFT JOIN plugin_pipeline_leases l USING(owner,sku,seller)
                WHERE q.owner=? AND q.state='needs_fields' ORDER BY q.due''',(owner,)).fetchall()
            remaining=len(rows)
            for row in rows:
                if row['lease_expires'] and row['lease_expires']>now:continue
                body=json.loads(row['body']);retry=body.get('repair_retry') or {}
                dependency=retry.get('failure_class') in ('network','remote_pending')
                dependency_since=retry.get('dependency_since',now)
                if dependency and now-dependency_since<21600:continue
                attempts=retry.get('attempts',0);first=retry.get('first_attempt_at')
                if not isinstance(attempts,int) or isinstance(attempts,bool) or attempts<0:continue
                queued_at=body.get('repair_wait_started_at',body.get('updated_at',body.get('requested_at')))
                queue_age=now-queued_at if isinstance(queued_at,(int,float)) and 0<queued_at<=now else 0
                age=now-first if isinstance(first,(int,float)) and 0<first<=now else 0
                reason=('repair_dependency_unavailable_over_6_hours' if dependency else
                        'repair_queue_wait_over_30_minutes' if attempts==0 and queue_age>=1800 else
                        'three_failed_repairs' if attempts>=3 else
                        'repair_wait_over_30_minutes' if age>=1800 else
                        'repair_capacity_pressure' if remaining>=36 and attempts>=2 and age>=600 else None)
                if not reason:continue
                c.execute('''INSERT INTO repair_cleanup_receipts(owner,sku,seller,at,reason,
                    previous_state,previous_body,previous_due,previous_attempts) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (owner,row['sku'],row['seller'],now,reason,row['state'],row['body'],row['due'],row['attempts']))
                body['repair_cleanup']={'at':now,'reason':reason,'attempts':attempts,'previous_state':'needs_fields'}
                body.update(reason='stalled_repair_parked',updated_at=now)
                c.execute("UPDATE plugin_pipeline SET state='needs_review',body=?,due=? WHERE owner=? AND sku=? AND seller=? AND state='needs_fields'",
                          (json.dumps(body),now,owner,row['sku'],row['seller']))
                remaining-=1;parked.append({'sku':row['sku'],'seller':row['seller'],'reason':reason})
        return {'state':'complete','parked':len(parked),'tasks':parked}
