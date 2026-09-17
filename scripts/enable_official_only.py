"""Audit/apply new-intent official-only routing; never rewrite existing publication journals."""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database, private_write
from flowhub.official_api import client
from flowhub.store_vat import verified_sample_profile


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    db=Database()
    path=db.directory/'store-vat.json'
    original=path.read_text() if path.exists() else '{}'
    profiles=json.loads(original)
    with db.connect() as c:
        stores=[dict(r) for r in c.execute('SELECT * FROM stores WHERE verified=1').fetchall()]
        samples={r['id']:[v[0] for v in c.execute("SELECT json_extract(body,'$.offer_id') FROM plugin_publications WHERE owner=? AND json_extract(body,'$.store_id')=? AND json_extract(body,'$.phase')='stock_verified' AND COALESCE(json_extract(body,'$.backend'),'maozi')='maozi' ORDER BY updated DESC LIMIT 3",(r['owner'],r['id'])).fetchall()] for r in stores}
    report=[]
    for store in stores:
        keys=db.open(store['secret']); entry={'store':store['name'],'official_credentials':bool(keys.get('client_id') and keys.get('api_key'))}
        if entry['official_credentials']:
            account=str(keys['client_id'])
            if account in profiles:entry['vat']='existing_profile'
            elif len(set(samples[store['id']]))<3:entry['vat']='insufficient_verified_samples'
            else:
                try:
                    async with client(db,keys) as api:
                        response=await api.post('/v5/product/info/prices',json={'filter':{'offer_id':samples[store['id']],'visibility':'ALL'},'limit':100})
                        response.raise_for_status()
                        profiles[account]=verified_sample_profile(account,samples[store['id']],response.json().get('items'))
                    entry['vat']='verified_same_account'
                except Exception as error:
                    entry['vat']='blocked:'+type(error).__name__
        report.append(entry)
        print(json.dumps(entry,ensure_ascii=False),flush=True)
    if args.apply:
        backup=db.directory/('official-only-before-'+str(int(time.time()))+'.json')
        private_write(backup,json.dumps({'store_vat':json.loads(original),'stores':[{k:r[k] for k in ('id','owner','config')} for r in stores]},ensure_ascii=False))
        if (path.read_text() if path.exists() else '{}') != original:
            raise RuntimeError('VAT profiles changed concurrently; retained old configuration')
        temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
        private_write(temporary,json.dumps(profiles,ensure_ascii=False,indent=2))
        os.replace(temporary,path)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for store in stores:
                config=json.loads(store['config'])|{'publication_backend':'official_only'}
                if c.execute('UPDATE stores SET config=? WHERE id=? AND owner=? AND config=?',
                             (json.dumps(config),store['id'],store['owner'],store['config'])).rowcount!=1:
                    raise RuntimeError('store config changed concurrently')
        print(json.dumps({'applied_stores':len(stores),'backup':str(backup),'journal_changes':0}))
    print(json.dumps({'verified_official_stores':sum(r['official_credentials'] for r in report),
                      'vat_ready_stores':sum(r.get('vat') in ('existing_profile','verified_same_account') for r in report)}))


if __name__=='__main__':
    asyncio.run(main())
