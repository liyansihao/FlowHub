"""Persistent, bounded admission from the verified source library. No browser fallback."""
import json
import re
import time
from ..source_library import SourceLibrary
from . import control


def schema(db):
    from ..plugin_pipeline import schema as queue_schema
    queue_schema(db)
    with db.connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS pipeline_capabilities(name TEXT PRIMARY KEY,state TEXT,body TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS pipeline_campaigns(owner TEXT PRIMARY KEY,enabled INTEGER NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS pipeline_admissions(owner TEXT,sku TEXT,seller TEXT,at REAL,body TEXT,PRIMARY KEY(owner,sku));
        CREATE TABLE IF NOT EXISTS plugin_routes(owner TEXT,sku TEXT,seller TEXT,store_id TEXT,expires REAL,run_id TEXT,PRIMARY KEY(owner,sku,seller));
        CREATE TABLE IF NOT EXISTS plugin_publication_permissions(owner TEXT,sku TEXT,seller TEXT,expires REAL,reason TEXT,PRIMARY KEY(owner,sku,seller));
        ''')


def asking_price(product):
    """A new asking-price decision may retain a captured price; never call it market freshness."""
    from ..plugin_detail import positive
    quote=product.get('proposed_sale_price') or {}
    if quote.get('currency')=='CNY' and positive(quote.get('value')):return float(quote['value'])
    value=str(product.get('current_price_display') or '').replace('\\u2009','').replace('\u2009','').strip()
    match=re.fullmatch(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*[¥￥]',value)
    return positive(match[1].replace(',','.')) if match else None


def asking_quote(product):
    from ..plugin_detail import positive
    price=asking_price(product)
    if price:return {'value':price,'currency':'CNY'}
    rub=positive(product.get('current_price_rub'))
    return {'value':rub,'currency':'RUB'} if rub else None


def admit_one(db, owner, now=None):
    now=time.time() if now is None else now
    schema(db)
    if control.paused(db,'seed'):return {'state':'paused'}
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM pipeline_campaigns WHERE owner=? AND enabled=1',(owner,)).fetchone()
        if not row:return {'state':'disabled'}
        policy=json.loads(row['body'])
        if policy.get('until') and now>=policy['until']:return {'state':'window_closed'}
        renew_campaign(c,owner,policy,now)
        dependency=c.execute("SELECT state,body FROM pipeline_capabilities WHERE name='qwen_review'").fetchone()
        if dependency and dependency['state']!='ready':return {'state':'awaiting_dependency','reason':'qwen_review','details':json.loads(dependency['body'])}
        # Remote processing has a separate bound, so accepted imports do not stop
        # preparation of the next products while waiting for platform visibility.
        remote_sql="state IN ('publishing','awaiting_remote') AND COALESCE(json_extract(body,'$.phase'),'') IN ('submitting','reconciling','sync_pending','stock_ready','stock_pending','manual_review')"
        remote=c.execute('SELECT count(*) FROM plugin_pipeline WHERE owner=? AND ('+remote_sql+')',(owner,)).fetchone()[0]
        active=c.execute("SELECT count(*) FROM plugin_pipeline WHERE owner=? AND state IN ('queued','evaluating','publishing') AND NOT ("+remote_sql+")",(owner,)).fetchone()[0]
        if remote>=policy.get('max_remote_pending',40):return {'state':'backpressure','remote_pending':remote}
        repairs=c.execute("SELECT count(*) FROM plugin_pipeline WHERE owner=? AND state='needs_fields'",(owner,)).fetchone()[0]
        publication_repairs=c.execute("SELECT count(*) FROM plugin_pipeline WHERE owner=? AND state='needs_fields' AND json_extract(body,'$.official_dossier_pending')=1",(owner,)).fetchone()[0]
        valuation_repairs=repairs-publication_repairs
        # Existing post-review dossier backlog has its own lane. Admit a small,
        # bounded fresh cohort instead of letting it stop all new acquisition.
        repair_limit=min(policy.get('max_repair_pending',48),8) if publication_repairs else policy.get('max_repair_pending',48)
        active+=c.execute("SELECT count(*) FROM plugin_pipeline q JOIN plugin_pipeline_leases l USING(owner,sku,seller) WHERE q.owner=? AND q.state='needs_fields' AND l.expires>?",(owner,now)).fetchone()[0]
        if active>=max(1,policy.get('max_inflight',12)-(1 if repairs else 0)):return {'state':'backpressure','active':active}
        promoted=promote_ready_candidate(c,db,owner,now)
        if promoted:return promoted
        total=c.execute('SELECT count(*) FROM pipeline_admissions WHERE owner=? AND json_extract(body,\'$.run_id\')=?',(owner,policy['run_id'])).fetchone()[0]
        if policy.get('max_admissions') and total>=policy['max_admissions']:return {'state':'admission_limit'}
        from .store_capacity import schema as capacity_schema
        capacity_schema(c)
        stores=[]
        for sid in policy['store_ids']:
            store=c.execute('SELECT id,config,secret FROM stores WHERE owner=? AND id=? AND verified=1',(owner,sid)).fetchone()
            if store and not c.execute("SELECT 1 FROM store_publication_capacity WHERE owner=? AND store_id=? AND state='blocked'",(owner,sid)).fetchone():
                config=json.loads(store['config'])
                from ..official_publication import backend
                if backend(config) != 'maozi':
                    credentials=db.open(store['secret'])
                    if not credentials.get('client_id') or not credentials.get('api_key'):continue
                if all(config.get(k) for k in ('shop_id','warehouse_id','watermark_id')):stores.append(store)
        if not stores:return {'state':'blocked','reason':'no_available_target_store'}
        # Alternate fresh discoveries and oldest backlog without starving either.
        order='DESC' if policy.get('mix_fresh_sources') and total%2==0 else 'ASC'
        # A bounded acceptance cohort must not get trapped between a growing
        # newest-first queue and a large oldest-first backlog. All gates above
        # and exclusions below still apply to these SKUs.
        cohort=json.dumps([str(s) for s in policy.get('acceptance_skus',[])][:100])
        rows=c.execute('''SELECT p.* FROM sourcing_products p WHERE p.owner=?
          AND json_extract(p.body,'$.coverage') IN ('storefront-page','maozi-exact-seller-page')
          AND json_extract(p.body,'$.source_relation.seller_id')=json_extract(p.body,'$.seller_id')
          AND json_array_length(json_extract(p.body,'$.source_relation.root_seeds'))>0
          AND NOT EXISTS(SELECT 1 FROM pipeline_admissions a WHERE a.owner=p.owner AND a.sku=p.sku)
          AND NOT EXISTS(SELECT 1 FROM plugin_pipeline q WHERE q.owner=p.owner AND q.sku=p.sku)
          AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.owner=p.owner AND j.source_key=p.sku)
          AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=p.owner AND b.source_key=p.sku)
          ORDER BY CASE WHEN p.sku IN (SELECT value FROM json_each(?)) THEN 0 ELSE 1 END,
          p.id '''+order,(owner,cohort))
        for row in rows:
            p=json.loads(row['body']);relation=p.get('source_relation') or {}
            if relation.get('seller_id')!=p.get('seller_id') or not relation.get('root_seeds'):continue
            # All source provenance and live delist checks still run before matching and writing.
            target=stores[total%len(stores)];key=(owner,row['sku'],row['seller'])
            stamp={'run_id':policy['run_id'],'at':now,'source_observed_at':p.get('collected_at'),'target_store':target['id']}
            quote=asking_quote(p) if policy.get('retain_captured_asking_price') else None
            if quote:
                p.setdefault('price_intent_history',[]).append({'at':now,'previous':p.get('proposed_sale_price'),'reference_observed_at':p.get('collected_at')})
                p['proposed_sale_price']={**quote,'observed_at':now,'source':'campaign-asking-price-decision','run_id':policy['run_id'],'reference_observed_at':p.get('collected_at')}
            from .repair import valuation_ready
            ready=valuation_ready(p)
            if not ready and valuation_repairs>=repair_limit:continue
            expires=min(now+policy.get('write_window_seconds',21600),policy.get('until') or float('inf'))
            c.execute('INSERT INTO pipeline_admissions VALUES(?,?,?,?,?)',(*key,now,json.dumps(stamp)))
            c.execute('INSERT INTO plugin_routes VALUES(?,?,?,?,?,?)',(*key,target['id'],expires,policy['run_id']))
            if policy.get('allow_unknown'):
                c.execute('INSERT INTO plugin_publication_permissions VALUES(?,?,?,?,?)',(*key,expires,'campaign explicit unknown shipping/follow permission'))
            c.execute("INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)",(*key,'queued' if ready else 'needs_fields',json.dumps({'requested_at':now,'submitted':False,'campaign_run_id':policy['run_id']}),now,0))
            SourceLibrary(db).put(owner,p,{'channel':'continuous-admission','run_id':policy['run_id']},connection=c)
            return {'state':'admitted','sku':row['sku'],'seller':row['seller']}
        if valuation_repairs>=repair_limit:
            return {'state':'backpressure','repair_pending':valuation_repairs,**({'publication_repair_pending':publication_repairs} if publication_repairs else {})}
        return {'state':'source_exhausted','reason':'no_new_bound_source_candidate'}



def promote_ready_candidate(c, db, owner, now):
    """New source facts can release an unmeasured candidate during repair backoff.

    Called in admission's capacity-checked write transaction. Existing decisions,
    publication repair and live repair leases stay with their respective lanes.
    """
    from .repair import valuation_ready
    has_reviews=c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_reviews'").fetchone()
    rows=c.execute("""SELECT q.*,p.body product FROM plugin_pipeline q
        JOIN sourcing_products p USING(owner,sku,seller)
        LEFT JOIN plugin_pipeline_leases l USING(owner,sku,seller)
        WHERE q.owner=? AND q.state='needs_fields' AND (l.expires IS NULL OR l.expires<=?)
        ORDER BY q.due""",(owner,now)).fetchall()
    for row in rows:
        key=(owner,row['sku'],row['seller']);body=json.loads(row['body'])
        if body.get('submitted') or body.get('evaluation_state') or body.get('pending_publication_fields'):continue
        if has_reviews and c.execute('SELECT 1 FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone():continue
        product=json.loads(row['product'])
        if not valuation_ready(product):continue
        if body.get('repair_retry'):body.setdefault('repair_history',[]).append(body.pop('repair_retry'))
        body.pop('error',None);body.pop('reason',None)
        body.update(repair_reason='valuation_inputs_ready',updated_at=now)
        c.execute("UPDATE plugin_pipeline SET state='queued',body=?,due=? WHERE owner=? AND sku=? AND seller=?",(json.dumps(body),now,*key))
        product.setdefault('listing_review',{})['pipeline']={'state':'queued',**body}
        SourceLibrary(db).put(owner,product,{'channel':'valuation-ready-promotion'},connection=c)
        return {'state':'promoted','sku':row['sku'],'seller':row['seller']}



def renew_campaign(c, owner, policy, now):
    """Only an active continuous campaign authorizes a new write window; keep every old one."""
    if not policy.get('continuous'):return
    expiry=min(now+policy.get('write_window_seconds',21600),policy.get('until') or float('inf'))
    for r in c.execute('SELECT * FROM plugin_routes WHERE owner=? AND run_id=? AND expires<=?',(owner,policy['run_id'],now)).fetchall():
        key=(owner,r['sku'],r['seller'])
        q=c.execute("SELECT state FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?",key).fetchone()
        if not q or q['state'] in ('selling','rejected'):continue
        old=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',key).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone() else None
        if old:
            record=json.loads(old[0]);record.setdefault('continuations',[]).append({'at':now,'previous_write_deadline':record.get('write_deadline'),'write_deadline':expiry,'reason':'active_continuous_campaign'})
            record['write_deadline']=expiry
            c.execute('UPDATE plugin_publications SET body=?,updated=? WHERE owner=? AND sku=? AND seller=?',(json.dumps(record),now,*key))
        c.execute('UPDATE plugin_routes SET expires=? WHERE owner=? AND sku=? AND seller=?',(expiry,*key))
        c.execute('UPDATE plugin_publication_permissions SET expires=? WHERE owner=? AND sku=? AND seller=?',(expiry,*key))


async def run(db):
    import asyncio
    schema(db)
    while True:
        with db.connect() as c:owners=[r[0] for r in c.execute('SELECT owner FROM pipeline_campaigns WHERE enabled=1')]
        for owner in owners:
            started=time.time()
            try:
                result=admit_one(db,owner)
            except Exception as error:
                result={'state':'error','reason':type(error).__name__}
            control.record(db,'seed',owner,'',started,result['state'],result)
        await asyncio.sleep(5)
