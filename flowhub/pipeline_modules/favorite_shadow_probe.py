"""One bounded read-only production observation; writes only a separate ledger.
Run with python -m flowhub.pipeline_modules.favorite_shadow_probe --data ... --output ...
No DELETE/POST request API is exposed by this worker.
"""
import argparse
import asyncio
import collections
import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from cryptography.fernet import Fernet
from ..maozi import MaoziPublisher
from .favorite_shadow import Ledger, inventory


async def run(data, output):
    data=Path(data).resolve();output=Path(output).resolve()
    if output == data or data in output.parents:
        raise ValueError('shadow output must be outside production data')
    output.mkdir(parents=True,exist_ok=True)
    cipher=Fernet((data/'master.key').read_bytes())
    def decrypt(body):return json.loads(cipher.decrypt(body.encode()))
    with sqlite3.connect((data/'flowhub.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=3) as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def rows(table):return [dict(r) for r in c.execute('SELECT * FROM '+table)] if table in tables else []
        stores=rows('stores');pubs=rows('plugin_publications');queues=rows('plugin_pipeline')
        details=rows('source_details');acqs=rows('acquisition_tasks');bindings=rows('acquisition_bindings')
    version=subprocess.check_output(['git','-C',str(data.parent),'rev-parse','HEAD'],text=True).strip()
    groups={}
    for r in stores:
        keys=decrypt(r['secret']);token=keys.get('erp_token')
        if not token:continue
        account=hashlib.sha256(token.encode()).hexdigest()
        group=groups.setdefault(account,dict(owners=set(),keys=keys,config=json.loads(r['config'])))
        group['owners'].add(r['owner'])
    indexed=collections.defaultdict(list)
    for p in pubs:
        body=json.loads(p['body']);phase=body.get('phase','unknown')
        # Every non-final publication is conservatively protected. A final one
        # remains unknown until durable dossiers and complete coverage exist.
        state='required' if phase in ('prepared','favorite_pending','ready') else 'unknown'
        indexed[(p['owner'],str(p['sku']))].append(dict(consumer='publication:'+json.dumps([p['owner'],p['sku'],p['seller']]),
            state=state,reason='publication:'+phase,business_version=str(p['updated']),released_at=None,evidence={'phase':phase}))
    for q in queues:
        phase=q['state'];state='required' if phase in ('queued','same_product_confirmed','not_listed') else 'unknown'
        indexed[(q['owner'],str(q['sku']))].append(dict(consumer='pipeline:'+json.dumps([q['owner'],q['sku'],q['seller']]),
            state=state,reason='pipeline:'+phase,business_version=hashlib.sha256(json.dumps(q,sort_keys=True).encode()).hexdigest(),released_at=None,evidence={'state':phase}))
    detail_index={r['key']:r for r in details}
    acq_index={r['key']:r for r in acqs}
    for b in bindings:
        r=acq_index.get(b['task_key'])
        if not r:continue
        body=decrypt(r['body']);stage=body.get('stage','unknown')
        indexed[(b['owner'],str(b['sku']))].append(dict(consumer='acquisition:'+r['key'],
            state='required' if stage in ('find_favorite','create_favorite','favorite_capacity','import_draft') else 'unknown',reason='acquisition:'+stage,
            business_version=str(r['version']),released_at=None,evidence={'stage':stage}))
    ledger=Ledger(output/'shadow.sqlite3');reports=[];observations=[]
    for account,g in groups.items():
        api=MaoziPublisher({'store':{'config':g['config'],'credentials':g['keys']}})
        async def get(page):
            async with asyncio.timeout(25):
                result=await api.erp('GET','/api.product.favorite/lists',params={'page':page,'page_size':100})
            await asyncio.sleep(.2)
            return result
        started=time.time()
        try:
            async with asyncio.timeout(180):found,stats=await inventory(get)
        except Exception as e:
            reports.append(dict(account=account,error_type=type(e).__name__,observed_at=time.time()))
            continue
        counts=collections.Counter();reasons=collections.Counter();unknown_reasons=collections.Counter()
        for fid,sku in found.items():
            consumers=[]
            for owner in g['owners']:
                consumers.extend(indexed[(owner,sku)])
                key=hashlib.sha256((owner+':'+account+':'+sku).encode()).hexdigest()
                d=detail_index.get(key)
                if d:
                    consumers.append(dict(consumer='source:'+key,state='required' if d['state']=='favorite_started' else 'unknown',
                        reason='source:'+d['state'],business_version=str(d['updated']),released_at=None,
                        evidence={'state':d['state']}))
            state=ledger.observe(account,fid,sku,time.time_ns(),consumers,business_version=version,
                                  coverage_complete=False,durable_snapshot=False,independent_import=False)
            counts[state]+=1
            reasons.update(set(x['reason'] for x in consumers if x['state']=='required'))
            unknown_reasons.update(set(x['reason'] for x in consumers if x['state']=='unknown'))
            if not consumers:unknown_reasons.update(['no_local_consumer_match'])
            observations.append(dict(account=account,favorite_id=fid,sku=sku,favorite_required=True,
                 classification=state,consumers=consumers,blocking_reason='active_dependency' if state=='required' else 'missing_complete_release_proof'))
        reports.append(dict(account=account,**stats,counts=dict(counts),dependency_reasons=dict(reasons),unknown_reasons=dict(unknown_reasons),
                            observed_at=time.time(),duration_seconds=time.time()-started))
    report=dict(at=time.time(),production_version=version,mode='shadow_read_only',accounts=reports,
                safe_delete_count=len(ledger.candidates()),new_safe_last_24h=sum(x['eligible_at']>=time.time()-86400 for x in ledger.candidates()),
                lifecycle_coverage='incomplete: production hooks not deployed; all releases disabled',
                observations=observations)
    target=output/('observation-'+str(time.time_ns())+'.json')
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (output/'latest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (output/'would-delete.json').write_text(json.dumps(ledger.candidates(),ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='observations'},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();asyncio.run(run(a.data,a.output))
