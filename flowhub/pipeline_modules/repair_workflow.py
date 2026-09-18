"""Resumable, bounded repair stages; the original pipeline owns the SKU lease.

This is a work journal, not a second publication queue. Source writes continue
to use SourceCollector's one-shot journal and never originate from this module.
"""
import asyncio
import hashlib
import json
import time

PUBLICATION_SQL="(COALESCE(json_extract(q.body,'$.official_dossier_pending'),0)=1 OR COALESCE(json_extract(q.body,'$.repair_full_dossier'),0)=1 OR COALESCE(json_array_length(json_extract(q.body,'$.pending_publication_fields')),0)>0 OR COALESCE(json_extract(q.body,'$.phase'),'')='awaiting_dossier')"


def config(db):
    path=db.directory/'repair-workflow.json'
    cfg={'enabled':False,'valuation_workers':1,'publication_workers':1,'valuation_queue_limit':16,
         'facts_timeout':60,'source_timeout':120,'validate_timeout':120}
    if path.exists():cfg.update(json.loads(path.read_text()))
    if any(cfg[k] not in (1,2) for k in ('valuation_workers','publication_workers')):
        raise ValueError('repair workers must remain bounded to one or two per lane')
    if not 1<=cfg['valuation_queue_limit']<=48:raise ValueError('invalid valuation queue bound')
    if any(not 1<=cfg[k]<=180 for k in ('facts_timeout','source_timeout','validate_timeout')):
        raise ValueError('invalid repair stage timeout')
    return cfg


def enabled(db):return config(db)['enabled']


def kind(body):
    return 'publication' if (body.get('official_dossier_pending') or body.get('repair_full_dossier')
        or body.get('pending_publication_fields') or body.get('phase')=='awaiting_dossier') else 'valuation'


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS repair_workflows(
        owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))''')
    c.execute('''CREATE TABLE IF NOT EXISTS repair_workflow_events(
        id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,at REAL,body TEXT)''')


def fingerprint(queue,store):
    return hashlib.sha256(json.dumps([queue.get('listing_control_id') or queue.get('requested_at'),
        (queue.get('lifecycle') or {}).get('repair_entries',0),store,kind(queue)],sort_keys=True).encode()).hexdigest()


async def run_one(module,db,owner,sku,seller):
    key=(owner,sku,seller);cfg=config(db);began=time.time()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE');schema(c)
        row=c.execute('SELECT body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        route=c.execute('SELECT store_id FROM plugin_routes WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        if not row or not route:return {'state':'waiting','reason':'repair_binding_missing','failure_class':'identity_mismatch'}
        queue=json.loads(row[0]);identity=fingerprint(queue,route[0]);lane=kind(queue)
        lease=c.execute('SELECT token FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        if not lease:raise BlockingIOError('repair requires original SKU lease')
        token=lease[0]
        old=c.execute('SELECT body FROM repair_workflows WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        work=json.loads(old[0]) if old else {}
        if work.get('identity')!=identity:
            if work:c.execute('INSERT INTO repair_workflow_events VALUES(NULL,?,?,?,?,?)',(*key,began,json.dumps({'event':'superseded','previous':work})))
            work={'identity':identity,'kind':lane,'stage':'facts','created_at':began,'last_progress_at':began,'attempts':{}}
        if work.get('next_at',0)>began:
            return {'state':'waiting','reason':work.get('reason','stage_backoff'),'retry_after':work['next_at']-began,
                    'workflow':work,'failure_class':work.get('failure_class','remote_pending')}
        if work.get('state')=='ready':
            return {'state':'ready','reason':'complete_dossier' if lane=='publication' else 'valuation_inputs_ready','workflow':work}
        if work.get('state')=='manual':return {'state':'manual','reason':work['reason'],'workflow':work,'missing_fields':work.get('missing_fields',[])}
        stage=work['stage'];work.update(state='running',started_at=began)
        c.execute('INSERT OR REPLACE INTO repair_workflows VALUES(?,?,?,?,?)',(*key,json.dumps(work),began))
    try:
        operation=module.run_stage(db,*key,stage=stage,purpose=lane,full_dossier=lane=='publication')
        from ..acquisition import enabled as acquisition_enabled
        result=await operation if stage=='source' and acquisition_enabled(db,sku) else await asyncio.wait_for(operation,cfg[stage+'_timeout'])
    except asyncio.CancelledError:
        # A restart retains the current stage and all source write intents.
        raise
    except Exception as error:
        from ..official_api import OfficialDeferred
        from .repair_retry import classify
        category='network' if isinstance(error,(TimeoutError,OfficialDeferred)) else classify({'steps':[{'reason':type(error).__name__+': '+str(error)[:150]}]})
        result={'state':'waiting','reason':type(error).__name__,'failure_class':category,'missing_fields':work.get('missing_fields',[])}
    now=time.time();work['attempts'][stage]=work['attempts'].get(stage,0)+1
    work.update(last_elapsed_seconds=round(now-began,3),updated_at=now,missing_fields=result.get('missing_fields',[]),reason=result['reason'])
    if result['state'] in ('ready','progress'):
        work.update(state=result['state'],next_at=now,last_progress_at=now,dependency_attempts=0,failures=0,failure_class=None)
        if result['state']=='progress':
            work['stage']=result['next_stage']
            work['next_at']=now+result.get('retry_after',0)
    else:
        category=result.get('failure_class') or 'missing_fields';work['failure_class']=category
        dependency=category in ('network','remote_pending','capacity','rate_deferred','operation_timeout','auth_expired')
        name='dependency_attempts' if dependency else 'failures';work[name]=work.get(name,0)+1
        manual=not dependency and (stage=='validate' or work[name]>=3 or category=='identity_mismatch')
        delay=result.get('retry_after') or (min(900,30*2**min(work[name]-1,5)) if dependency else 300)
        work.update(state='manual' if manual else 'waiting',next_at=now+delay)
        result.update(state='manual' if manual else 'waiting',retry_after=delay)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT token FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        q=c.execute('SELECT body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        r=c.execute('SELECT store_id FROM plugin_routes WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        if not current or current[0]!=token or not q or not r or fingerprint(json.loads(q[0]),r[0])!=identity:
            raise BlockingIOError('repair ownership changed; checkpoint not advanced')
        c.execute('UPDATE repair_workflows SET body=?,updated=? WHERE owner=? AND sku=? AND seller=?',(json.dumps(work),now,*key))
        c.execute('INSERT INTO repair_workflow_events VALUES(NULL,?,?,?,?,?)',(*key,now,json.dumps({'stage':stage,'elapsed_seconds':work['last_elapsed_seconds'],'state':work['state'],'reason':result['reason'],'missing_fields':work['missing_fields']})))
    return result|{'workflow':work}
