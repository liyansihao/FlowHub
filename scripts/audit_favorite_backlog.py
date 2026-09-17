"""Read-only, exact-identity audit. No pipeline/journal reset or ERP writes."""
import argparse,asyncio,collections,hashlib,json,sqlite3,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub import plugin_publication
from flowhub.pipeline_modules.favorite_lookup import PublicationSourceAdapter
from flowhub.pipeline_modules.request_bridge import PublicationBridge,MeasuredTransport,close_requests
from flowhub.pipeline_modules.transport import StepTransport
import httpx

async def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--remote',action='store_true');a=p.parse_args()
    db=Database();out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
    with db.connect() as c:
        rows=c.execute("SELECT q.owner,q.sku,q.seller,q.body,p.body publication FROM plugin_pipeline q JOIN plugin_publications p USING(owner,sku,seller) WHERE q.state='awaiting_remote' AND json_extract(q.body,'$.reason')='favorite_visibility_exhausted'").fetchall()
    journal=sqlite3.connect('file:'+str(db.directory/'plugin-production.sqlite3')+'?mode=ro',uri=True)
    report=json.loads(out.read_text()) if out.exists() else {'started_at':time.time(),'read_only':True,'items':[]}
    done={(x['sku'],x['seller']) for x in report['items']}
    rows=[row for row in rows if (row['sku'],row['seller']) not in done]
    semaphore=asyncio.Semaphore(2)
    async def audit(row):
        async with semaphore:
            b=json.loads(row['body']);pub=json.loads(row['publication']);offer=pub['offer_id'];plan=pub['plan']
            j=journal.execute('SELECT phase,details FROM zero_stock_tests WHERE offer_id=? AND sku=? AND shop_id=?',(offer,row['sku'],str(plan['shop_id']))).fetchone()
            details=json.loads(j[1]) if j else {}
            with db.connect() as c:
                clean=[dict(r) for r in c.execute('SELECT favorite_id,state,updated FROM favorite_cleanup_receipts WHERE owner=? AND sku=?',(row['owner'],row['sku']))]
                store=c.execute('SELECT secret FROM stores WHERE owner=? AND id=?',(row['owner'],pub['store_id'])).fetchone()
            item={'sku':row['sku'],'seller':row['seller'],'shop_id':str(plan['shop_id']),'offer_id':offer,'journal_phase':j[0] if j else None,
                  'favorite_attempts':details.get('favorite_attempts'),'favorite_acknowledged_at':details.get('favorite_acknowledged_at'),
                  'favorite_id':details.get('favorite_id'),'cleanup_receipts':clean,'classification':'awaiting_remote_audit',
                  'next_action':'read exact favorite views, import log and original offer'}
            if a.remote and store:
                keys=db.open(store['secret']);bridge=PublicationBridge(plugin_publication.ROOT,execute=False,token=keys['erp_token'])
                t=StepTransport(MeasuredTransport(bridge),namespace=hashlib.sha256(keys['erp_token'].encode()).hexdigest())
                try:
                    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=t) as client:
                        port=PublicationSourceAdapter(client)
                        favorite=await port.favorite_id(row['sku']);item['remote_favorite_id']=favorite
                        product=await port.find_product(str(plan['shop_id']),offer)
                        item['exact_offer_visible']=product is not None
                        if product:item.update(classification='original_offer_visible',next_action='reconcile original offer; do not resubmit')
                        elif favorite:item.update(classification='favorite_visible',next_action='normal recovery on original journal')
                        else:
                            logs=await port._rows('/api.product.import_logs/index',sku=row['sku'])
                            imported=any(str(r.get('sku'))==row['sku'] for r in logs);item['source_imported']=imported
                            item.update(classification='imported_elsewhere_or_delayed' if imported else 'acknowledged_but_absent',next_action='resolve import identity; no recreation' if imported else 'low-frequency read-only observation; do not renew exhausted write budget')
                except Exception as e:item.update(classification='read_unavailable',error_type=type(e).__name__,error_reason=str(e)[:400],next_action='retry audit after API recovers')
                item['timings']=t.timings
            report['items'].append(item);report['updated_at']=time.time();report['counts']=dict(collections.Counter(x['classification'] for x in report['items']))
            out.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'audited':len(report['items']),'total':len(rows),'sku':item['sku'],'classification':item['classification']}),flush=True)
    try:await asyncio.gather(*(audit(row) for row in rows))
    finally:journal.close();await close_requests()
    print(json.dumps(report.get('counts',{})))

if __name__=='__main__':asyncio.run(main())
