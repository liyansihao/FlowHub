"""Bounded, resumable Maozi dossier supplementation. Never changes queue state or publishes."""
import argparse,asyncio,collections,json,os,secrets,time
from pathlib import Path
from flowhub.db import Database
from flowhub.control import load_runtime_env
from flowhub.manual_reviews import publication_started
from flowhub.pipeline_modules import repair
from flowhub.pipeline_modules.dossier import repaired_review
from flowhub.source_detail import SourceCollector
from flowhub.maozi import MaoziPublisher

class BatchCollector(SourceCollector):
    """Use the existing Python Maozi client for read-only data, paced per process.
    Acquisition writes retain the existing globally paced guarded bridge.
    """
    read_gate=None;next_read=0;recovery_gate=None
    async def call(self,path,method='GET',query=None,body=None):
        if method!='GET':return await super().call(path,method,query,body)
        if type(self).read_gate is None:type(self).read_gate=asyncio.Lock()
        async with type(self).read_gate:
            await asyncio.sleep(max(0,type(self).next_read-time.monotonic()))
            type(self).next_read=time.monotonic()+.5
        return await MaoziPublisher(self.c).erp(method,path,params=query,body=body)
    async def recover_draft(self,data):
        if type(self).recovery_gate is None:type(self).recovery_gate=asyncio.Lock()
        async with type(self).recovery_gate:return await super().recover_draft(data)

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--workers',type=int,default=3);parser.add_argument('--limit',type=int)
    args=parser.parse_args()
    if not 1<=args.workers<=4:raise ValueError('workers must be 1..4')
    load_runtime_env();owner=os.environ['FLOWHUB_REVIEW_OWNER'];db=Database()
    out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=True);out.chmod(0o700)
    manifest=out/'manifest.json';results_path=out/'results.jsonl'
    if not manifest.exists():
        selected=[];excluded=collections.Counter()
        with db.connect() as c:
            rows=c.execute("SELECT q.*,p.body product FROM plugin_pipeline q JOIN sourcing_products p USING(owner,sku,seller) WHERE q.owner=? AND q.state IN ('needs_review','needs_fields') ORDER BY q.sku,q.seller",(owner,)).fetchall()
            for row in rows:
                missing=repair.missing_fields(json.loads(row['product']))
                if not missing:excluded['already_complete']+=1;continue
                q=json.loads(row['body'])
                if publication_started(c,owner,row['sku'],row['seller'],q):excluded['existing_publication']+=1;continue
                if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,row['sku'])).fetchone():excluded['blocked']+=1;continue
                selected.append({'owner':owner,'sku':row['sku'],'seller':row['seller'],'state':row['state'],'missing':missing})
        manifest.write_text(json.dumps({'created_at':time.time(),'owner':owner,'selected':selected,'excluded':dict(excluded)},ensure_ascii=False,indent=2))
    plan=json.loads(manifest.read_text())
    if plan['owner']!=owner:raise ValueError('owner mismatch')
    prior=[json.loads(line) for line in results_path.read_text().splitlines()] if results_path.exists() else []
    done={(r['sku'],r['seller']) for r in prior if not r.get('busy')}
    targets=[r for r in plan['selected'] if (r['sku'],r['seller']) not in done]
    if args.limit:targets=targets[:args.limit]
    print(json.dumps({'selected':len(plan['selected']),'remaining':len(targets),'excluded':plan['excluded'],'apply':args.apply}),flush=True)
    if not args.apply:return
    repair.SourceCollector=BatchCollector
    gate=asyncio.Semaphore(args.workers);write_gate=asyncio.Lock();finished=0
    async def one(row):
        nonlocal finished
        async with gate:
            key=(owner,row['sku'],row['seller']);token='supplement-'+secrets.token_hex(16);started=time.time()
            result={'sku':row['sku'],'seller':row['seller'],'started_at':started}
            claimed=False;heartbeat=None
            try:
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    qr=c.execute('SELECT state,body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()
                    if not qr or qr['state'] not in ('needs_review','needs_fields'):
                        result['skipped']='state_changed';return
                    q=json.loads(qr['body'])
                    if publication_started(c,*key,q):result['skipped']='existing_publication';return
                    if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,row['sku'])).fetchone():result['skipped']='blocked';return
                    if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(*key,time.time())).fetchone():result['busy']=True;return
                    c.execute('INSERT OR REPLACE INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(*key,token,time.time()+120));claimed=True
                    p=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()
                    rr=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone()
                    before=json.loads(p[0]);old_review=json.loads(rr[0]) if rr else None
                backup=out/f"before-{row['sku']}-{row['seller']}.json"
                if not backup.exists():
                    backup.write_text(json.dumps({'queue':dict(qr),'product':before,'review':old_review},ensure_ascii=False));backup.chmod(0o600)
                async def renew():
                    while True:
                        await asyncio.sleep(25)
                        with db.connect() as c:
                            c.execute('UPDATE plugin_pipeline_leases SET expires=? WHERE owner=? AND sku=? AND seller=? AND token=?',(time.time()+120,*key,token))
                heartbeat=asyncio.create_task(renew())
                result['before']=repair.missing_fields(before)
                outcome=await asyncio.wait_for(repair.PriceRepairModule().run(db,*key,full_dossier=True),240)
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    latest=json.loads(c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0])
                    now_queue=c.execute('SELECT state,body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()
                    result['queue_unchanged']=dict(now_queue)==dict(qr)
                    if old_review and old_review.get('state')=='needs_review' and now_queue['state']=='needs_review':
                        synced=repaired_review(old_review,latest,allow_manual=True)
                        if synced and synced['candidate']!=old_review.get('candidate'):
                            result['review_dossier_synced']=bool(c.execute('UPDATE plugin_reviews SET body=? WHERE owner=? AND sku=? AND seller=? AND body=?',(json.dumps(synced),*key,rr[0])).rowcount)
                    d=latest.get('plugin_detail') or {};result.update(outcome)
                    result.update(after=repair.missing_fields(latest),attributes=len(d.get('attributes') or []),weight_g=d.get('weight_g'),dimensions_mm=d.get('dimensions_mm'),dossier_observed_at=d.get('observed_at'),steps=((latest.get('collection_evidence') or {}).get('last_repair') or {}).get('steps',[]))
            except Exception as error:
                result['error']=type(error).__name__
            finally:
                if heartbeat:
                    heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
                if claimed:
                    with db.connect() as c:c.execute('DELETE FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND token=?',(*key,token))
                result['elapsed']=round(time.time()-started,2)
                async with write_gate:
                    with results_path.open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
                    finished+=1
                    if finished%10==0 or finished==len(targets):print(json.dumps({'finished':finished,'batch':len(targets),'last_sku':row['sku'],'last_after':result.get('after'),'last_error':result.get('error')}),flush=True)
    await asyncio.gather(*(one(row) for row in targets))
    all_results={}
    for line in results_path.read_text().splitlines():
        r=json.loads(line);all_results[(r['sku'],r['seller'])]=r
    summary={'selected':len(plan['selected']),'finished':len(all_results),'full_dossier':sum('after' in r and not any(k in r['after'] for k in ('weight_g','dimensions_mm','attributes','fresh_dossier')) for r in all_results.values()),'all_fields_ready':sum(r.get('after')==[] for r in all_results.values()),'missing':dict(collections.Counter(k for r in all_results.values() for k in r.get('after',[]))),'errors':dict(collections.Counter(r['error'] for r in all_results.values() if r.get('error'))),'skipped':dict(collections.Counter(r['skipped'] for r in all_results.values() if r.get('skipped'))),'busy':sum(bool(r.get('busy')) for r in all_results.values()),'synced_manual':sum(bool(r.get('review_dossier_synced')) for r in all_results.values()),'finished_at':time.time()}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':asyncio.run(main())
