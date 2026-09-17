"""FlowHub protocol 2: execute each central ERP command at most once.

Credentials exist only in memory during a request. No automatic ERP retries.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import threading
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.error import HTTPError, URLError
from agent import Client, save
from runtime_identity import capture, report_loop

READS = {'/api.shop/lists', '/api.product.favorite/lists',
         '/api.product.import_logs/index', '/api.product.online/lists',
         '/api.product.online/get_stock'}
POSTS = {'/api.chrome/sku3', '/api.product.favorite/toggle',
         '/api.selection.follow/import', '/api.product.online/batch_update_stock',
         '/api.product.online/sync_shop'}


def execute(client, command):
    url = urlsplit(command['url'])
    method = command['method']
    if (url.scheme != 'https' or url.netloc != 'api.maozierp.com'
            or url.fragment or '%' in url.path or '..' in url.path
            or not ((method == 'GET' and url.path in READS)
                    or (method == 'POST' and url.path in POSTS))):
        return {'error': 'endpoint_rejected'}
    if set(k.lower() for k in command['headers']) - {
            'accept','accept-language','authorization','client','content-type'}:
        return {'error': 'headers_rejected'}
    body = command.get('body')
    req = Request(command['url'], data=body.encode() if body else None,
                  headers=command['headers'], method=method)
    try:
        try:
            response = client.opener.open(req, timeout=15)
        except HTTPError as exc:
            # Redirects are returned as responses, never followed with credentials.
            response = exc
        with response:
            data = response.read(5_000_001)
            if len(data) > 5_000_000:
                return {'error': 'response_too_large'}
            return {'status': response.status, 'body': data.decode('utf-8'),
                    'headers': {'retry-after': response.headers.get('retry-after','')}}
    except Exception:
        return {'error': 'remote_outcome_unknown'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True)
    args=p.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    runtime=capture('erp-agent',Path(__file__).resolve().parent,{'erp':2})
    # Same lock as phase 1, so both versions cannot run concurrently.
    handle=args.config.with_suffix('.lock').open('a+b')
    if os.name=='nt':
        import msvcrt
        if handle.tell()==0:handle.write(b'0');handle.flush()
        handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    client=Client(config['url'],config['token'])
    threading.Thread(target=report_loop,args=(Client(config['url'],config['token']),runtime,threading.Event()),daemon=True).start()
    ledger=sqlite3.connect(args.config.with_suffix('.commands.sqlite3'))
    ledger.execute('PRAGMA synchronous=FULL')
    ledger.execute('CREATE TABLE IF NOT EXISTS dispatched(id TEXT PRIMARY KEY)')
    pending=args.config.with_suffix('.erp-result.json')
    backoff=1
    print('FlowHub production agent v2 ready. Waiting for centrally authorized commands.',flush=True)
    while True:
        try:
            if pending.exists():
                data=json.loads(pending.read_text(encoding='utf-8'))
                try:client.call('/v2/erp/complete',data)
                except HTTPError as exc:
                    if exc.code!=409:raise
                pending.unlink()
            task=client.call('/v2/erp/claim')['task']
            if task:
                # Intent precedes begin and network dispatch. Neither a restarted
                # agent nor a lost HTTP response can execute this ID twice.
                try:
                    with ledger:ledger.execute('INSERT INTO dispatched VALUES(?)',(task['id'],))
                except sqlite3.IntegrityError:
                    time.sleep(1);continue
                try:command=client.call('/v2/erp/begin',task)
                except HTTPError as exc:
                    if exc.code==409:continue
                    raise
                result=execute(client,command)
                del command
                save(pending,{**task,'result':result})
                print('ERP command completed: '+task['id'],flush=True)
            backoff=1;time.sleep(.5)
        except (HTTPError,URLError,TimeoutError,OSError):
            exc=sys.exc_info()[1]
            if isinstance(exc,HTTPError) and exc.code in (401,403):
                print('Device authorization rejected.',flush=True);return 2
            print('Coordinator unavailable; retrying. ERP writes will not be replayed.',flush=True)
            time.sleep(backoff);backoff=min(30,backoff*2)


if __name__=='__main__':
    try:sys.exit(main() or 0)
    except KeyboardInterrupt:pass
