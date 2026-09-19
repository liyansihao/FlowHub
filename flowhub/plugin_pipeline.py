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
from .official_api import OfficialDeferred


def schema(db):
    control.schema(db)
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_pipeline(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT,due REAL,attempts INTEGER DEFAULT 0,PRIMARY KEY(owner,sku,seller))')
        c.execute('CREATE INDEX IF NOT EXISTS plugin_pipeline_state_due ON plugin_pipeline(state,due)')
        c.execute('CREATE INDEX IF NOT EXISTS plugin_pipeline_owner_state_due ON plugin_pipeline(owner,state,due)')
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


def readback_delay(body, phase, verified=False, submission_priority=False, *, native_follow=False):
    """Persisted phase-based polling: useful writes stay immediate, remote waits back off."""
    if verified:
        body.pop('readback_schedule',None)
        return 0
    if phase not in ('submitting','reconciling','sync_pending','stock_pending','favorite_pending'):
        body.pop('readback_schedule',None)
        return 0
    previous=body.get('readback_schedule') or {}
    repeat=previous.get('repeat',0)+1 if previous.get('phase')==phase else 0
    base,cap=(60,600) if phase=='favorite_pending' else (20,120) if phase=='stock_pending' else ((180,900) if submission_priority else (60,240))
    if native_follow:base,cap=15,60
    delay=min(cap,base*2**min(repeat,4))
    body['readback_schedule']={'phase':phase,'repeat':repeat,'delay_seconds':delay,
                               'first_wait_at':previous.get('first_wait_at',time.time())}
    return delay


def claim(db, lane=None, *, target=None, run_paused=False, repair_kind=None, repair_stage=None, acquisition_route=None):
    from .pipeline_modules import repair_workflow
    staged_repairs=repair_workflow.enabled(db)
    if acquisition_route is not None and (not isinstance(acquisition_route,bool) or repair_stage!='acquire'):
        raise ValueError('invalid_acquisition_route')
    if repair_stage not in (None,'validate','acquire') or (repair_stage and repair_kind!='publication'):
        raise ValueError('invalid_repair_stage')
    if repair_kind not in (None,'publication','valuation') or (repair_kind and lane!='seed_repair'):
        raise ValueError('invalid_repair_kind')
    if target is not None and (len(target)!=3 or not all(isinstance(v,str) and v for v in target)):
        raise ValueError('exact_pipeline_identity_required')
    if run_paused and (target is None or lane not in ('seed_repair','review')):
        raise ValueError('paused_execution_requires_exact_repair_or_review_target')
    schema(db)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if lane not in (None, 'review', 'submit', 'reconcile', 'reconcile_history', 'seed_repair'):
            raise ValueError('unknown pipeline lane')
        module = 'seed' if lane == 'seed_repair' else 'review' if lane == 'review' else 'publication'
        if lane and not run_paused and c.execute('SELECT 1 FROM pipeline_module_control WHERE module=? AND paused=1', (module,)).fetchone():
            return False
        lane_sql = {
            None: '',
            'review': " AND q.state IN ('queued','evaluating')",
            'seed_repair': " AND q.state='needs_fields'",
            'submit': " AND q.state='publishing' AND COALESCE(json_extract(q.body,'$.phase'),'') NOT IN (" + ','.join('?' for _ in RECONCILE) + ')',
            'reconcile_history': " AND q.state IN ('publishing','awaiting_remote') AND json_extract(q.body,'$.phase')='manual_review'",
            'reconcile': " AND COALESCE(json_extract(q.body,'$.phase'),'')!='manual_review' AND q.state IN ('publishing','awaiting_remote') AND json_extract(q.body,'$.phase') IN (" + ','.join('?' for _ in RECONCILE) + ')',
        }[lane]
        if repair_kind:
            if staged_repairs:lane_sql+=' AND '+('' if repair_kind=='publication' else 'NOT ')+repair_workflow.PUBLICATION_SQL
            else:lane_sql += " AND COALESCE(json_extract(q.body,'$.official_dossier_pending'),0)" + ('=1' if repair_kind=='publication' else '!=1')
        if repair_stage:
            lane_sql += " AND COALESCE(json_extract(q.body,'$.repair_workflow.stage'),'facts')"+ ('=' if repair_stage=='validate' else '!=')+"'validate'"
        route_args = []
        if acquisition_route is not None:
            from .acquisition import routing
            route_sql, route_args = routing(db,c)
            lane_sql += " AND " + ("" if acquisition_route else "NOT ") + route_sql
        now = time.time()
        r=c.execute("""SELECT q.* FROM plugin_pipeline q JOIN users u ON u.id=q.owner
            LEFT JOIN plugin_pipeline_leases l ON l.owner=q.owner AND l.sku=q.sku AND l.seller=q.seller
            WHERE u.active=1 AND q.state IN ('queued','evaluating','publishing','needs_fields','awaiting_remote') AND q.due<=?
            AND (l.expires IS NULL OR l.expires<=?)""" + lane_sql + (" AND q.owner=? AND q.sku=? AND q.seller=?" if target else "") + """
            ORDER BY CASE
              WHEN json_extract(q.body,'$.phase') IN ('stock_pending','stock_ready','stock_verified') THEN 0
              WHEN json_extract(q.body,'$.phase') IN ('reconciling','sync_pending','submitting') THEN 1
              WHEN json_extract(q.body,'$.phase')='ready' THEN 2
              WHEN q.state='publishing' THEN 3
              ELSE 4 END,
              CASE WHEN q.state='needs_fields' AND json_extract(q.body,'$.repair_workflow.stage')='validate' THEN 0 ELSE 1 END,
              CASE WHEN q.state='needs_fields' AND json_extract(q.body,'$.pending_publication_fields') IS NOT NULL THEN 0 WHEN q.state='needs_fields' AND json_extract(q.body,'$.evaluation_state') IS NULL AND json_extract(q.body,'$.repair_retry') IS NULL THEN 1 ELSE 2 END,q.due LIMIT 1""", (now, now, *(RECONCILE if lane in ('submit','reconcile') else ()),*route_args,*(target or ()))).fetchone()
        if not r:return False
        if r['state']=='needs_fields' and not (staged_repairs and repair_kind=='publication'):
            campaign=c.execute('SELECT body FROM pipeline_campaigns WHERE owner=? AND enabled=1',(r['owner'],)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_campaigns'").fetchone() else None
            limit=json.loads(campaign[0]).get('max_inflight',12) if campaign else 12
            active=c.execute("SELECT count(*) FROM plugin_pipeline WHERE owner=? AND state IN ('queued','evaluating','publishing') AND COALESCE(json_extract(body,'$.phase'),'') NOT IN ('submitting','reconciling','sync_pending','stock_ready','stock_pending','manual_review')",(r['owner'],)).fetchone()[0]
            active+=c.execute("SELECT count(*) FROM plugin_pipeline q JOIN plugin_pipeline_leases l USING(owner,sku,seller) WHERE q.owner=? AND q.state='needs_fields' AND l.expires>?"+(' AND NOT '+repair_workflow.PUBLICATION_SQL if staged_repairs else ''),(r['owner'],now)).fetchone()[0]
            if active>=limit:return False
        row=dict(r);key=(row['owner'],row['sku'],row['seller']);token=secrets.token_hex(16)
        c.execute('INSERT OR REPLACE INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(*key,token,now+120))
    return row,key,token


async def tick(db, lane=None, *, target=None, run_paused=False, repair_kind=None, repair_stage=None, acquisition_route=None):
    from .pipeline_modules.database_work import run as database_work
    claim_task=asyncio.create_task(asyncio.to_thread(claim,db,lane,target=target,run_paused=run_paused,
        repair_kind=repair_kind,repair_stage=repair_stage,acquisition_route=acquisition_route))
    try:
        claimed=await asyncio.shield(claim_task)
    except asyncio.CancelledError:
        claimed=await claim_task
        if claimed:await database_work(release_lease,db,claimed[1],claimed[2])
        raise
    if not claimed:return False
    row,key,token=claimed
    async def renew():
        while True:
            await asyncio.sleep(30)
            await database_work(renew_lease,db,key,token)
    heartbeat=asyncio.create_task(renew())
    started=time.time()
    state=row['state'];body=json.loads(row['body']);attempts=row['attempts'];delay=0;lock_wait=False;dependency_wait=False;repair_progress=False
    try:
        from .follow_publication import release_repair, selected as follow_selected, clear_dossier
        if state=='needs_fields' and release_repair(db,key,body):
            state='queued'
        elif state=='needs_fields':
            from .pipeline_modules.repair import PriceRepairModule
            result=await PriceRepairModule().run(db,*key)
            body['repair_reason']=result['reason']
            if result.get('workflow'):body['repair_workflow']=result['workflow']
            if result['state']=='ready':
                state='queued';body.pop('error',None);body.pop('reason',None)
                body.pop('repair_manual',None);body.pop('repair_dependency',None)
                if body.pop('official_dossier_pending',False):
                    for field in ('needs_dossier','missing_fields','pending_publication_fields','repair_full_dossier'):
                        body.pop(field,None)
                    body.pop('phase',None)
                if body.get('repair_retry'):
                    body.setdefault('repair_history',[]).append(body.pop('repair_retry'))
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_reviews'").fetchone():
                        from .pipeline_modules.dossier import synchronize_repaired_review
                        if synchronize_repaired_review(c,key,allow_manual=True):
                            synced=json.loads(c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0])
                            state='publishing' if synced['state']=='matched' else 'needs_review'
                            body.update(evaluation_state=synced['state'],repair_reason='dossier_synced_to_approved_review' if state=='publishing' else 'dossier_synced_to_manual_review')
                            if state=='needs_review':body['reason']=synced.get('result',{}).get('reason','manual_same_product_review')
                            body.pop('pending_publication_fields',None)
                        else:
                            from .pipeline_modules.repair_queue import preserve_review
                            preserve_review(c,db,key,body)
                    if state=='publishing' and (result.get('workflow') or {}).get('kind')=='publication':
                        from .dossier_gate import record as record_gate
                        body['dossier_gate_hash']=record_gate(c,key)
            elif result.get('workflow'):
                if result['state']=='progress':
                    repair_progress=True;delay=result.get('retry_after',0)
                elif result['state']=='manual':
                    state='needs_review';body.update(reason='repair_input_required: '+result['reason'],repair_manual=True)
                else:
                    delay=result.get('retry_after',60)
                    body['repair_dependency']=result.get('failure_class') or result['workflow'].get('failure_class')
            else:
                repair=body.setdefault('repair_retry',{})
                from .pipeline_modules.repair_retry import schedule
                delay,exhausted=schedule(repair,result,started)
                if exhausted:
                    state='needs_review';body['reason']='repair_retry_exhausted: '+result['reason']
                    repair['exhausted_at']=time.time();delay=0
        elif state in ('queued','evaluating'):
            if body.get('same_product_only'):
                from .identity_review import evaluate as evaluate_identity
                report=await evaluate_identity(db,*key)
                body.pop('force_identity_review',None)
            elif body.get('force_full_evaluation'):
                report=await evaluate(db,*key,force=True)
                body.pop('force_full_evaluation',None)
            else:report=await evaluate(db,*key)
            reason=report.get('reason') or report.get('result',{}).get('reason')
            decision=report.get('result',{}).get('evidence',{}).get('source',{}).get('comparebot',{}).get('decision',{})
            if reason in ('qwen_not_configured','qwen_billing_blocked','qwen_authentication_blocked') or decision.get('qwen_review'):
                with db.connect() as c:
                    c.execute('CREATE TABLE IF NOT EXISTS pipeline_capabilities(name TEXT PRIMARY KEY,state TEXT,body TEXT,updated REAL)')
                    c.execute('INSERT OR REPLACE INTO pipeline_capabilities VALUES(?,?,?,?)',('qwen_review','ready' if decision.get('qwen_review') else 'blocked',json.dumps({'reason':reason,'sku':key[1]}),time.time()))
            body['evaluation_state']=report['state']
            if report['state']=='matched':
                state='publishing'
                from .acquisition import enabled as acquisition_enabled
                native_follow=follow_selected(db,key)
                if native_follow:clear_dossier(body)
                if not native_follow and acquisition_enabled(db,key[1]):
                    with db.connect() as c:
                        exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone()
                        existing=exists and c.execute('SELECT 1 FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',key).fetchone()
                    if not existing:
                        state='needs_fields'
                        body.update(repair_full_dossier=True,official_dossier_pending=True,reason='publication_dossier_gate')
                origin=(report.get('candidate') or {}).get('origin') or {}
                if not native_follow and origin.get('weight_first_valuation'):
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
            if state=='awaiting_remote' and result['phase'] in ('ready','favorite_pending'):
                state='publishing'
            body['submitted']=bool(body.get('submitted')) or result['phase'] in ('reconciling','sync_pending','stock_ready','stock_pending','stock_verified')
            if result.get('verified'):state='selling'
            elif result.get('needs_dossier'):
                state='needs_fields';delay=300
                body.pop('error',None)
                body.update(repair_full_dossier=True,official_dossier_pending=True,
                            pending_publication_fields=result.get('missing_fields',[]))
            elif result.get('retryable_readback'):
                state='awaiting_remote';delay=3600 if result.get('reason')=='favorite_visibility_exhausted' else 900 if result.get('phase')=='manual_review' else 300
                if result.get('reason')=='favorite_visibility_exhausted':
                    unchanged=(result.get('favorite_recovery') or {}).get('unchanged_checks',0)
                    delay=min(21600,3600*2**min(max(0,unchanged-1),3))
            elif result['phase'] in ('failed','manual_review'):state='needs_review'
            if state not in ('awaiting_remote','needs_fields'):
                policy,native_follow=await database_work(publication_readback_policy,db,key,body.get('campaign_run_id'))
                body['readback_policy']=policy
                delay=readback_delay(body,result['phase'],bool(result.get('verified')),policy['submission_priority'],native_follow=native_follow)
        body.pop('dependency_wait',None)
        if state!='needs_fields':body.pop('error',None)
    except OfficialDeferred as error:
        delay=max(1,error.until-time.time());body['dependency_wait']=error.reason;dependency_wait=True
    except BlockingIOError:
        # No ERP attempt occurred: retain resumable state without reporting an old error.
        delay=5;lock_wait=True
    except ValueError as error:
        state='needs_fields' if str(error) in ('price_evidence_stale','plugin_facts_missing_or_stale','pricing_facts_missing','fresh_comparebot_approval_required') else 'awaiting_remote' if str(error)=='hour_window_closed' and body.get('submitted') else 'needs_review';body['error']=str(error)
        if state=='awaiting_remote':delay=300
    except Exception as error:
        attempts+=1;body['error']=type(error).__name__+': '+str(error)[:200];delay=min(300,2**min(attempts,8))
        # An uncertain publication remains resumable in its immutable ERP journal.
        if state not in ('publishing','awaiting_remote') and attempts>=8:state='needs_review'
    except asyncio.CancelledError:
        await database_work(release_lease,db,key,token)
        raise
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat,return_exceptions=True)
    return await database_work(finish,db,row,key,token,state,body,attempts,delay,lane,started,
                               lock_wait,dependency_wait,repair_progress)


def publication_readback_policy(db,key,campaign_run_id):
    from .pipeline_modules.throughput_policy import readback_policy
    from .follow_publication import selected
    with db.connect() as c:
        campaign=c.execute('SELECT body FROM pipeline_campaigns WHERE owner=? AND enabled=1',(key[0],)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_campaigns'").fetchone() else None
        policy=json.loads(campaign[0]) if campaign else {}
        priority=policy.get('submission_priority',False) and campaign_run_id==policy.get('run_id')
        result=readback_policy(c,key[0],priority,time.time())
    return result,selected(db,key)


def finish(db,row,key,token,state,body,attempts,delay,lane,started,lock_wait,dependency_wait,repair_progress):
    if state=='publishing':
        with db.connect() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone():
                latest=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',key).fetchone()
                if latest:
                    publication=json.loads(latest[0])
                    if publication.get('phase'):
                        body['phase']=publication['phase']
                        body['submitted']=bool(body.get('submitted')) or publication['phase'] in ('submitting','reconciling','sync_pending','stock_ready','stock_pending','stock_verified')
    if state=='needs_review' and not body.get('repair_manual'):
        from .manual_reviews import publication_started
        with db.connect() as c:
            if not publication_started(c,*key,body):body['same_product_only']=True
    if state=='needs_fields':
        if row['state']!='needs_fields':body['repair_wait_started_at']=time.time()
        else:body.setdefault('repair_wait_started_at',body.get('updated_at',started))
    else:body.pop('repair_wait_started_at',None)
    if lane=='reconcile_history' and state=='awaiting_remote' and body.get('phase')=='manual_review':delay=max(delay,900)
    from .pipeline_modules.lifecycle import track
    track(body,row['state'],state,time.time(),attempted=not (lock_wait or dependency_wait))
    if repair_progress:body['lifecycle']['last_progress_at']=time.time()
    body['updated_at']=time.time()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token)).fetchone():
            return False
        if state=='needs_review' and body.get('same_product_only') and not body.get('repair_manual'):
            from .identity_review import reconcile_pending
            state=reconcile_pending(c,key,body) or state
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
    control.record(db,'seed' if row['state']=='needs_fields' else 'review' if row['state'] in ('queued','evaluating') else 'publication',key[0],key[1],started,'waiting_lock' if lock_wait else 'waiting_dependency' if dependency_wait else state,{'lane':lane,'phase':body.get('phase'),'reason':body.get('dependency_wait') if dependency_wait else None if lock_wait else body.get('error')})
    return not lock_wait


def release_lease(db,key,token):
    with db.connect() as c:
        c.execute('DELETE FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token))


def renew_lease(db,key,token):
    with db.connect() as c:
        c.execute('UPDATE plugin_pipeline_leases SET expires=? WHERE owner=? AND sku=? AND seller=? AND token=?',
                  (time.time()+120,*key,token))
