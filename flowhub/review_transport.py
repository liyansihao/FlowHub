"""Per-request HTTPS routing for a review host whose system DNS is unreliable.
Uses the existing proxy and TLS certificate verification; never changes system DNS.
"""
import asyncio
import base64
import gzip
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlencode, urlsplit

import httpx

_CACHE = {}


def _quote(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('\n','\\n').replace('\r','\\r')+'"'


async def request(client, config, method, *, params=None, body=None):
    if body is not None and config.get('FLOWHUB_REVIEW_PAYLOAD_ENCODING')=='gzip-base64' and (params or {}).get('mode') in ('refresh','delta'):
        packed=gzip.compress(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode())
        body={'encoding':'gzip-base64','payload':base64.b64encode(packed).decode()}
    endpoint=config['FLOWHUB_REVIEW_SYNC_URL']
    headers={'x-sync-token':config['FLOWHUB_REVIEW_SYNC_TOKEN']}
    if config.get('FLOWHUB_REVIEW_NETWORK')=='proxy':
        proxy=config.get('FLOWHUB_REVIEW_PROXY') or os.environ.get('HTTPS_PROXY')
        if not proxy:raise ValueError('Review proxy is not configured')
        async with httpx.AsyncClient(proxy=proxy,trust_env=False,timeout=60) as routed:
            return await routed.request(method,endpoint,params=params,headers=headers,json=body)
    if config.get('FLOWHUB_REVIEW_NETWORK')!='proxy_doh':
        return await client.request(method,endpoint,params=params,headers=headers,json=body)
    host=urlsplit(endpoint).hostname
    if urlsplit(endpoint).scheme!='https' or not host or not host.endswith('.workers.dev'):
        raise ValueError('Encrypted DNS routing is restricted to the configured workers.dev review host')
    proxy=config.get('FLOWHUB_REVIEW_PROXY') or os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    if not proxy:raise ValueError('Review host routing requires the configured proxy')
    cached=_CACHE.get(host)
    if not cached or cached[1]<=time.time():
        async with httpx.AsyncClient(proxy=proxy,trust_env=False,timeout=15) as resolver:
            r=await resolver.get('https://dns.google/resolve',params={'name':host,'type':'A'})
            r.raise_for_status()
            answers=[a for a in r.json().get('Answer',[]) if a.get('type')==1 and ipaddress.ip_address(a['data']).is_global]
            if not answers:raise ValueError('Review host DNS returned no public IPv4 address')
            _CACHE[host]=(answers[0]['data'],time.time()+min(300,max(30,answers[0].get('TTL',60))))
    ip=_CACHE[host][0]
    url=endpoint+('?' + urlencode(params) if params else '')
    # Credentials go through stdin, never process arguments, logs or shell expansion.
    lines=['silent','show-error','max-time = 60','url = '+_quote(url),'proxy = '+_quote(proxy),'connect-to = '+_quote(f'{host}:443:{ip}:443'),'request = '+_quote(method),'header = '+_quote('x-sync-token: '+headers['x-sync-token']),'write-out = "\\n%{http_code}"']
    path=None
    try:
        if body is not None:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',prefix='flowhub-review-',delete=False) as f:
                path=Path(f.name);json.dump(body,f,ensure_ascii=False,separators=(',',':'))
            lines+=['header = "content-type: application/json"','data-binary = '+_quote('@'+str(path))]
        process=await asyncio.create_subprocess_exec('curl','--config','-',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:stdout,stderr=await process.communicate(('\n'.join(lines)+'\n').encode())
        except BaseException:
            if process.returncode is None:process.kill()
            await process.wait();raise
        if process.returncode:
            _CACHE.pop(host,None)
            raise httpx.ConnectError('Review HTTPS route failed: '+stderr.decode(errors='replace')[:300])
        content,status=stdout.rsplit(b'\n',1)
        return httpx.Response(int(status),content=content,request=httpx.Request(method,url))
    finally:
        if path:path.unlink(missing_ok=True)
