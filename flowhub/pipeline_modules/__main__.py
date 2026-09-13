"""Local module controls: python -m flowhub.pipeline_modules status|pause|resume."""
import argparse
import collections
import json
import time
from ..db import Database
from . import control


def status(db, since=None):
    control.schema(db)
    since=time.time()-3600 if since is None else since
    with db.connect() as c:
        switches={r[0]:bool(r[1]) for r in c.execute('SELECT module,paused FROM pipeline_module_control')}
        rows=[dict(r) for r in c.execute('SELECT * FROM pipeline_module_events WHERE finished>=?',(since,))]
        queue=[dict(r) for r in c.execute('SELECT state,body FROM plugin_pipeline')] if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_pipeline'").fetchone() else []
        publications=[json.loads(r[0]) for r in c.execute('SELECT body FROM plugin_publications')] if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone() else []
    modules={}
    for module in control.MODULES:
        samples=[r for r in rows if r['module']==module]
        durations=sorted(r['finished']-r['started'] for r in samples)
        modules[module]={'paused':switches.get(module,False),'operations':len(samples),
                         'p50_seconds':durations[len(durations)//2] if durations else None,
                         'p95_seconds':durations[min(len(durations)-1,int(len(durations)*.95))] if durations else None}
    sold=[]
    for r in publications:
        if r.get('verified') and any(e.get('to')=='stock_verified' and e['at']>=since for e in r.get('events',[])):
            sold.append({'source_sku':r['sku'],'sku':r['product']['sku'],'shop':r['store_name'],'run_id':r.get('active_run_id') or r.get('run_id')})
    from .admission import schema
    schema(db)
    with db.connect() as c:
        campaigns=[{'owner':r['owner'],'enabled':bool(r['enabled']),**json.loads(r['body'])} for r in c.execute('SELECT * FROM pipeline_campaigns')]
        capabilities=[dict(r)|{'body':json.loads(r['body'])} for r in c.execute('SELECT * FROM pipeline_capabilities')]
        source_enabled=bool(c.execute('SELECT 1 FROM sourcing_settings WHERE enabled=1').fetchone()) if c.execute("SELECT 1 FROM sqlite_master WHERE name='sourcing_settings'").fetchone() else False
    waiting=[json.loads(r['body']) for r in queue if r['state'] not in ('selling','rejected')]
    ages=[max(0,time.time()-b.get('requested_at',time.time())) for b in waiting]
    alerts=[]
    from .source_loop import status as source_status
    sources={p['owner']:source_status(db,p['owner']) for p in campaigns}
    if not source_enabled:alerts.append('source_acquisition_disabled')
    if waiting and not any(r['module']=='publication' and r['outcome']=='selling' and r['finished']>=time.time()-600 for r in rows):alerts.append('no_verified_sale_in_10_minutes')
    return {'sources':sources,'capabilities':capabilities,'campaigns':campaigns,'alerts':alerts,'oldest_queue_seconds':max(ages,default=0),'at':time.time(),'since':since,'target_per_hour':40,'modules':modules,
            'queue':dict(collections.Counter(r['state'] for r in queue)),
            'verified_sales':sold,'verified_count':len(sold),
            'note':'Operation timing is not product throughput; only stock_verified + selling counts.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['status','pause','resume','retry-dependencies'])
    parser.add_argument('module',nargs='?',choices=control.MODULES)
    args=parser.parse_args();db=Database()
    if args.action=='retry-dependencies':
        from .admission import schema
        schema(db)
        with db.connect() as c:
            c.execute("UPDATE pipeline_capabilities SET state='checking',updated=? WHERE name='qwen_review'",(time.time(),))
            c.execute("UPDATE plugin_pipeline SET state='queued',due=? WHERE state='awaiting_dependency'",(time.time(),))
    elif args.action!='status':
        if not args.module:parser.error('module required')
        control.set_paused(db,args.module,args.action=='pause')
    print(json.dumps(status(db),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
