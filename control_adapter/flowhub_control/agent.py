import argparse
import fcntl
import json
import logging
import os
import shutil
import ssl
import time
import urllib.request
from pathlib import Path
from .controller import Controller, MacService, PRODUCTION
from .reader import Reader
from .storage import Store


def exchange(url, token, payload):
    if not url.startswith('https://'): raise ValueError('https_required')
    data=json.dumps(payload).encode()
    if len(data)>3_000_000: raise ValueError('exchange_payload_too_large')
    request=urllib.request.Request(url+'/api/system/v1/agent/exchange',data=data,
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').exists() else None
    # Never follow redirects carrying the device bearer token to another origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl): return None
    opener=urllib.request.build_opener(NoRedirect,urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
    with opener.open(request,timeout=20) as response:
        return json.loads(response.read(1_000_000))


def tick(config, reader, controller, store, transport=exchange):
    errors=[]
    for name, fn in [('projection',reader.project),('logs',reader.logs)]:
        try: fn()
        except Exception as e: errors.append({'component':name,'type':type(e).__name__})
    pending=store.get('pending_command')
    receipt=controller.execute(pending) if pending else None
    try:
        status=controller.status()
        activity=reader.activity()
        progress=reader.progress()
    except Exception as e:
        status={'state':'unknown','observed_at':time.time(),'error_type':type(e).__name__,'controls_enabled':False}
        activity=[];progress={}
    status['disk_free_bytes']=shutil.disk_usage(reader.data).free
    status['collection_errors']=errors
    batch=store.batch()
    pending_counts=store.pending_counts()
    for row in batch: pending_counts[row['kind']]-=1
    progress['upload_pending_after_batch']=pending_counts
    payload={'deployment':config['deployment'],'status':status,'activity':activity,'progress':progress,
             'entities':[{'kind':r['kind'],'key':r['key'],'body':json.loads(r['body'])} for r in batch],
             'receipt':receipt}
    result=transport(config['url'],config['token'],payload)
    store.ack(batch)
    # Atomic local handoff; server only releases next command after terminal receipt.
    store.put('pending_command',result.get('command'))
    return {'uploaded':len(batch),'state':status['state'],'errors':errors,'command':(result.get('command') or {}).get('id')}


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--once',action='store_true');args=p.parse_args()
    config_path=Path(args.config)
    if config_path.stat().st_mode & 0o077: raise SystemExit('private config must be mode 600')
    config=json.loads(config_path.read_text())
    if Path(config['production']).resolve()!=PRODUCTION: raise SystemExit('sole production path required')
    directory=Path(config['state_directory']);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if directory.resolve()==PRODUCTION/'data' or PRODUCTION/'data' in directory.resolve().parents:
        raise SystemExit('adapter state must be outside production data')
    with (directory/'agent.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        store=Store(directory/'control.sqlite3');reader=Reader(PRODUCTION/'data',store)
        controller=Controller(reader,store,MacService(),enabled=config.get('controls_enabled',False))
        failures=0
        while True:
            started=time.monotonic()
            try:
                result=tick(config,reader,controller,store);failures=0
                print(json.dumps({'at':time.time(),**result}),flush=True)
            except Exception as e:
                failures+=1
                print(json.dumps({'at':time.time(),'error_type':type(e).__name__,'failure_count':failures}),flush=True)
                if args.once: raise SystemExit(1)
            if args.once: break
            interval=min(120,15*2**min(failures,3))
            time.sleep(max(1,interval-(time.monotonic()-started)))

if __name__=='__main__': main()
