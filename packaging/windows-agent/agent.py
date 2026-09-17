"""Python 3.12+ stdlib-only FlowHub acceptance agent for Windows/macOS."""
import argparse,getpass,json,os,platform,sys,time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import build_opener,ProxyHandler,HTTPRedirectHandler,Request
from urllib.error import HTTPError,URLError

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def base_url(value):
    p=urlsplit(value)
    if p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('Use only the coordinator HTTPS origin')
    if p.scheme!='https' and not (p.scheme=='http' and p.hostname in ('127.0.0.1','localhost','::1')):
        raise ValueError('Remote coordinator requires HTTPS; do not disable certificate validation')
    return value.rstrip('/')

def save(path,value):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        if os.name!='nt':os.chmod(tmp,0o600)
        json.dump(value,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

class Client:
    def __init__(self,url,token=None):
        self.url=base_url(url);self.token=token
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
    def call(self,path,body=None):
        headers={'Content-Type':'application/json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        req=Request(self.url+path,data=json.dumps(body or {}).encode(),headers=headers,method='POST')
        with self.opener.open(req,timeout=25) as response:return json.load(response)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--enroll',action='store_true');parser.add_argument('--url');parser.add_argument('--once',action='store_true')
    args=parser.parse_args();path=args.config;path.parent.mkdir(parents=True,exist_ok=True)
    lock_handle=path.with_suffix('.lock').open('a+b')
    if os.name=='nt':
        import msvcrt
        if lock_handle.tell()==0:lock_handle.write(b'0');lock_handle.flush()
        lock_handle.seek(0);msvcrt.locking(lock_handle.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock_handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if args.enroll:
        if path.exists():raise ValueError('Existing device config: revoke old device before enrolling again')
        client=Client(args.url or input('Coordinator HTTPS URL: ').strip())
        result=client.call('/v1/enroll',{'code':getpass.getpass('One-time enrollment code: '),'platform':platform.system()})
        save(path,{'url':client.url,**result});print('Enrolled. Mode: acceptance_only');return
    config=json.loads(path.read_text(encoding='utf-8'));client=Client(config['url'],config['token'])
    result_file=path.with_suffix('.pending.json');backoff=5
    while True:
        try:
            client.call('/v1/heartbeat')
            if result_file.exists():
                pending=json.loads(result_file.read_text(encoding='utf-8'))
                try:client.call('/v1/complete',pending)
                except HTTPError as e:
                    if e.code!=409:raise
                    print('Previous lease expired; coordinator owns recovery.',flush=True)
                result_file.unlink()
            task=client.call('/v1/claim')['task']
            if task:
                if task['kind']!='probe':raise ValueError('Unsupported task kind; production execution is not enabled')
                client.call('/v1/heartbeat',{'task_id':task['id'],'lease':task['lease']})
                pending={'task_id':task['id'],'lease':task['lease'],'challenge':task['payload']['challenge'],'platform':platform.system()}
                save(result_file,pending);client.call('/v1/complete',pending);result_file.unlink()
                print('Acceptance task completed: '+task['id'],flush=True)
            elif args.once:print('Connected. No pending acceptance task.',flush=True)
            backoff=5
            if args.once:return
            time.sleep(30)
        except (HTTPError,URLError,TimeoutError,OSError) as e:
            if isinstance(e,HTTPError) and e.code in (401,403):
                print('Device authorization rejected; stop and contact coordinator administrator.',flush=True);return 2
            print('Connection unavailable; retry in '+str(backoff)+'s ('+type(e).__name__+').',flush=True)
            if args.once:return 1
            time.sleep(backoff);backoff=min(300,backoff*2)

if __name__=='__main__':
    try:sys.exit(main() or 0)
    except KeyboardInterrupt:pass
