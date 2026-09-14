"""Explicit per-product requests; persistent evaluate/publish continuation, independent of discovery."""
import asyncio
import json
import secrets
import time

from .pipeline_modules.review import ReviewModule
from .pipeline_modules.publication import PublicationModule
from .pipeline_modules import control

evaluate = ReviewModule().run
advance = PublicationModule().run
from .source_library import SourceLibrary


def schema(db):
    control.schema(db)
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_pipeline(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT,due REAL,attempts INTEGER DEFAULT 0,PRIMARY KEY(owner,sku,seller))')
        c.execute('CREATE TABLE IF NOT EXISTS plugin_pipeline_leases(owner TEXT,sku TEXT,seller TEXT,token TEXT,expires REAL,PRIMARY KEY(owner,sku,seller))')


def enqueue(db, owner, sku, seller):
    if not sku.isdigit() or not seller.isdigit():raise ValueError('numeric_identity_required')
    schema(db)
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone():raise ValueError('source_missing')
        c.execute('INSERT OR IGNORE INTO plugin_pipeline(owner,sku,seller,state,body,due) VALUES(?,?,?,?,?,?)',
                  (owner,sku,seller,'queued',json.dumps({'requested_at':time.time(),'submitted':False}),time.time()))
        r=c.execute('SELECT state,body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
    return {'sku':sku,'state':r['state'],**json.loads(r['body'])}


RECONCILE = ('submitting','reconciling','sync_pending','stock_ready','stock_pending','stock_verified','favorite_pending','manual_review')


def readback_delay(body, phase, verified=False, submission_priority=False):
    """Persisted phase-based polling: useful writes stay immediate, remote waits back off."""
    if verified:
        body.pop('readback_schedule',None)
        return 0
    if phase not in ('submitting','reconciling','sync_pending','stock_pending'):
        return 0
    previous=body.get('readback_schedule') or {}
    repeat=previous.get('repeat',0)+1 if previous.get('phase')==phase else 0
    base,cap=(20,120) if phase=='stock_pending' else ((180,900) if submission_priority else (60,240))
    delay=min(cap,base*2**min(repeat,4))
    body['readback_schedule']={'phase':phase,'repeat':repeat,'delay_seconds':delay}
    return delay


async def tick(db, lane=None):
    schema(db)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if lane not in (None, 'review', 'submit', 'reconcile', 'seed_repair'):
            raise ValueError('unknown pipeline lane')
        module = 'seed' if lane == 'seed_repair' else 'review' if lane == 'review' else 'publication'
        if lane and c.execute('SELECT 1 FROM pipeline_module_control WHERE module=? AND paused=1', (module,)).fetchone():
            return False
        lane_sql = {
            None: '',
            'review': " AND q.state IN ('queued','evaluating')",
            'seed_repair': " AND q.state='needs_fields'",
            'submit': " AND q.state='publishing' AND COALESCE(json_extract(q.body,'$.phase'),'') NOT IN (" + ','.join('?' for _ in RECONCILE) + ')',
            'reconcile': " AND q.state IN ('publishing','awaiting_remote') AND json_extract(q.body,'$.phase') IN (" + ','.join('?' for _ in RECONCILE) + ')',
        }[lane]
        now = time.time()
        r=c.execute("""SELECT q.* FROM plugin_pipeline q JOIN users u ON u.id=q.owner
            LEFT JOIN plugin_pipeline_leases l ON l.owner=q.owner AND l.sku=q.sku AND l.seller=q.seller
            WHERE u.active=1 AND q.state IN ('queued','evaluating','publishing','needs_fields','awaiting_remote') AND q.due<=?
            AND (l.expires IS NULL OR l.expires<=?)""" + lane_sql + """
            ORDER BY CASE
              WHEN json_extract(q.body,'$.phase') IN ('stock_pending','stock_ready','stock_verified') THEN 0
              WHEN json_extract(q.body,'$.phase') IN ('reconciling','sync_pending','submitting','favorite_pending') THEN 1
              WHEN json_extract(q.body,'$.phase')='ready' THEN 2
              WHEN q.state='publishing' THEN 3
              ELSE 4 END,q.due LIMIT 1""", (now, now, *(RECONCILE if lane in ('submit','reconcile') else ()))).fetchone()
        if not r:return False
        if r['state']=='needs_fields':
            campaign=c.execute('SELECT body FROM pipeline_campaigns WHERE owner=? AND enabled=1',(r['owner'],)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_campaigns'").fetchone() else None
            limit=json.loads(campaign[0]).get('max_inflight',12) if campaign else 12
            active=c.execute("SELECT count(*) FROM plugin_pipeline WHERE owner=? AND state IN ('queued','evaluating','publishing') AND COALESCE(json_extract(body,'$.phase'),'') NOT IN ('submitting','reconciling','sync_pending','stock_ready','stock_pending','manual_review')",(r['owner'],)).fetchone()[0]
            if active>=limit:return False
        row=dict(r);key=(row['owner'],row['sku'],row['seller']);token=secrets.token_hex(16)
        c.execute('INSERT OR REPLACE INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(*key,token,now+120))
    async def renew():
        while True:
            await asyncio.sleep(30)
            with db.connect() as c:
                c.execute('UPDATE plugin_pipeline_leases SET expires=? WHERE owner=? AND sku=? AND seller=? AND token=?',
                          (time.time()+120,*key,token))
    heartbeat=asyncio.create_task(renew())
    started=time.time()
    state=row['state'];body=json.loads(row['body']);attempts=row['attempts'];delay=0
    try:
        if state=='needs_fields':
            from .pipeline_modules.repair import PriceRepairModule
            result=await PriceRepairModule().run(db,*key)
            body['repair_reason']=result['reason']
            if result['state']=='ready':
                state='queued';body.pop('error',None);body.pop('reason',None)
                if body.get('repair_retry'):
                    body.setdefault('repair_history',[]).append(body.pop('repair_retry'))
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_reviews'").fetchone():
                        from .pipeline_modules.dossier import synchronize_repaired_review
                        if synchronize_repaired_review(c,key):
                            state='publishing'
                            body.update(evaluation_state='matched',repair_reason='dossier_synced_to_approved_review')
                            body.pop('pending_publication_fields',None)
                        else:
                            c.execute('DELETE FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key)
            else:
                repair=body.setdefault('repair_retry',{})
                repair['attempts']=int(repair.get('attempts',0))+1
                repair.setdefault('first_attempt_at',started)
                repair.update(last_attempt_at=started,missing_fields=result.get('missing_fields',[]),reason=result['reason'])
                delay=min(3600,300*2**min(repair['attempts']-1,4))
                repair['delay_seconds']=delay
                if repair['attempts']>=6:
                    state='needs_review';body['reason']='repair_retry_exhausted: '+result['reason']
                    repair['exhausted_at']=time.time();delay=0
        elif state in ('queued','evaluating'):
            report=await evaluate(db,*key)
            reason=report.get('reason') or report.get('result',{}).get('reason')
            decision=report.get('result',{}).get('evidence',{}).get('source',{}).get('comparebot',{}).get('decision',{})
            if reason in ('qwen_not_configured','qwen_billing_blocked','qwen_authentication_blocked') or decision.get('qwen_review'):
                with db.connect() as c:
                    c.execute('CREATE TABLE IF NOT EXISTS pipeline_capabilities(name TEXT PRIMARY KEY,state TEXT,body TEXT,updated REAL)')
                    c.execute('INSERT OR REPLACE INTO pipeline_capabilities VALUES(?,?,?,?)',('qwen_review','ready' if decision.get('qwen_review') else 'blocked',json.dumps({'reason':reason,'sku':key[1]}),time.time()))
            body['evaluation_state']=report['state']
            if report['state']=='matched':
                state='publishing'
                origin=(report.get('candidate') or {}).get('origin') or {}
                if origin.get('weight_first_valuation'):
                    from .pipeline_modules.repair import missing_fields
                    pending=missing_fields(origin)
                    if pending:
                        state='needs_fields'
                        body.update(reason='publication_dossier_pending',pending_publication_fields=pending)
                    else:
                        body.pop('pending_publication_fields',None)
                        body.pop('reason',None)
            elif report['state']=='running':state='evaluating';delay=5
            elif report['state']=='error':raise RuntimeError(report.get('reason','evaluation_error'))
            else:
                state=report['state'];body['reason']=report.get('reason') or report.get('result',{}).get('reason')
                if body['reason'] in ('qwen_not_configured','qwen_billing_blocked','qwen_authentication_blocked'):
                    state='awaiting_dependency'
                elif body['reason'] in ('qwen_request_failed','qwen_rate_limited'):
                    state='queued';delay=300

        else:
            result=await advance(db,*key);body.update(result)
            body['submitted']=bool(body.get('submitted')) or result['phase'] in ('reconciling','sync_pending','stock_ready','stock_pending','stock_verified')
            if result.get('verified'):state='selling'
            elif result.get('retryable_readback'):state='awaiting_remote';delay=300
            elif result['phase'] in ('failed','manual_review'):state='needs_review'
            if state!='awaiting_remote':
                with db.connect() as c:
                    campaign=c.execute('SELECT body FROM pipeline_campaigns WHERE owner=? AND enabled=1',(key[0],)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_campaigns'").fetchone() else None
                policy=json.loads(campaign[0]) if campaign else {}
                priority=policy.get('submission_priority',False) and body.get('campaign_run_id')==policy.get('run_id')
                delay=readback_delay(body,result['phase'],bool(result.get('verified')),priority)
        if state!='needs_fields':body.pop('error',None)
    except BlockingIOError:
        delay=1
    except ValueError as error:
        state='needs_fields' if str(error) in ('price_evidence_stale','plugin_facts_missing_or_stale','pricing_facts_missing','fresh_comparebot_approval_required') else 'awaiting_remote' if str(error)=='hour_window_closed' and body.get('submitted') else 'needs_review';body['error']=str(error)
        if state=='awaiting_remote':delay=300
    except Exception as error:
        attempts+=1;body['error']=type(error).__name__+': '+str(error)[:200];delay=min(300,2**min(attempts,8))
        # An uncertain publication remains resumable in its immutable ERP journal.
        if state!='publishing' and attempts>=8:state='needs_review'
    except asyncio.CancelledError:
        with db.connect() as c:
            c.execute('DELETE FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token))
        raise
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat,return_exceptions=True)
    if state=='publishing':
        with db.connect() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone():
                latest=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',key).fetchone()
                if latest:
                    publication=json.loads(latest[0])
                    if publication.get('phase'):
                        body['phase']=publication['phase']
                        body['submitted']=bool(body.get('submitted')) or publication['phase'] in ('submitting','reconciling','sync_pending','stock_ready','stock_pending','stock_verified')
    body['updated_at']=time.time()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token)).fetchone():
            return False
        c.execute('DELETE FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token))
        c.execute('UPDATE plugin_pipeline SET state=?,body=?,due=?,attempts=? WHERE owner=? AND sku=? AND seller=?',(state,json.dumps(body),time.time()+delay,attempts,*key))
        product=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        if product:
            p=json.loads(product[0]);r=p.setdefault('listing_review',{})
            r['pipeline']={'state':state,**body}
            if body.get('error')=='price_evidence_stale':
                r.update(label='来源价格已过期，待补价后重新测算',publication_ready=False,missing_fields=['fresh_sale_price'])
            if body.get('error')=='target_quota_unavailable':
                r.update(label='店铺额度不足，发布请求未发送',publication_ready=False)
            SourceLibrary(db).put(key[0],p,{'channel':'plugin-pipeline','state':state},connection=c)
    control.record(db,'seed' if row['state']=='needs_fields' else 'review' if row['state'] in ('queued','evaluating') else 'publication',key[0],key[1],started,state,{'lane':lane,'phase':body.get('phase'),'reason':body.get('error')})
    return True
