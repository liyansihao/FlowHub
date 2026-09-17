"""Independent supervised ERP and compute workers; credentials remain private."""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError,URLError
from agent import Client,save


def terminate(child):
    if child and child.poll() is None:
        child.terminate()
        try:child.wait(timeout=5)
        except subprocess.TimeoutExpired:child.kill();child.wait()


def compute_loop(root,path,config,stop,children,capability):
    pending=path.with_suffix('.compute-result.json')
    while not stop.is_set():
        worker=None
        try:
            env=os.environ.copy();env.pop('DASHSCOPE_API_KEY',None)
            env['PYTHON_DOTENV_DISABLED']='1'
            if (root/'model-cache').exists():
                env['HF_HOME']=str(root/'model-cache')
                env['HF_HUB_OFFLINE']='1'
            worker=subprocess.Popen([sys.executable,'-u',str(root/'compute_worker.py')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                text=True,encoding='utf-8',env=env)
            children['compute']=worker
            lines=queue.Queue()
            def read_output(process,output):
                for line in process.stdout:output.put(line)
                output.put(None)
            threading.Thread(target=read_output,args=(worker,lines),daemon=True).start()
            print('Loading compute model; ERP continues independently.',flush=True)
            ready=json.loads(lines.get(timeout=300))
            if not ready.get('ready'):raise RuntimeError('Model readiness failed')
            cap={'version':3,'slots':1,'kinds':['rank','screen','dossier'],
                 'cpu_count':ready['cpu_count'],'accelerator':ready['accelerator']}
            client=Client(config['url'],config['token'])
            client.call('/v3/compute/register',cap)
            capability['value']=cap
            print('FlowHub v3 ready: supplier search + image ranking + AI review. Device: '+ready['accelerator'],flush=True)
            while not stop.is_set():
                try:
                    if pending.exists():
                        try:client.call('/v3/compute/complete',json.loads(pending.read_text(encoding='utf-8')))
                        except HTTPError as exc:
                            if exc.code!=409:raise
                        pending.unlink()
                    task=client.call('/v3/compute/claim')['task']
                    if task:
                        worker.stdin.write(json.dumps(task)+'\n');worker.stdin.flush()
                        line=lines.get(timeout=140)
                        if line is None:raise RuntimeError('Compute process exited')
                        result=json.loads(line)
                        save(pending,{k:task[k] for k in ('id','lease','digest')}|{'result':result})
                        print('Compute finished: '+task['kind']+' '+task['id'],flush=True)
                    stop.wait(.5)
                except HTTPError as exc:
                    if exc.code in (401,403):stop.set();return
                    stop.wait(3)
                except (URLError,TimeoutError):stop.wait(3)
        except Exception as exc:
            if isinstance(exc,HTTPError) and exc.code in (401,403):stop.set()
            print('Compute unavailable ('+type(exc).__name__+'); restarting compute only.',flush=True)
        finally:
            capability['value']={'version':3,'slots':1,'kinds':[],
                                 'cpu_count':os.cpu_count() or 1,'accelerator':'unavailable'}
            terminate(worker)
        stop.wait(10)


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True)
    args=p.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    lock=args.config.with_suffix('.compute.lock').open('a+b')
    if os.name=='nt':
        import msvcrt
        if lock.tell()==0:lock.write(b'0');lock.flush()
        lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    root=Path(__file__).resolve().parent;stop=threading.Event();children={};capability={}
    def heartbeat():
        client=Client(config['url'],config['token'])
        while not stop.wait(5):
            cap=capability.get('value')
            if cap is None:continue
            try:client.call('/v3/compute/register',cap)
            except HTTPError as exc:
                if exc.code in (401,403):stop.set()
            except (URLError,TimeoutError,OSError):pass
    threading.Thread(target=heartbeat,daemon=True).start()
    threading.Thread(target=compute_loop,args=(root,args.config,config,stop,children,capability),daemon=True).start()
    try:
        while not stop.is_set():
            erp=children.get('erp')
            if erp is None or erp.poll() is not None:
                if erp is not None and erp.returncode==2:
                    print('ERP authorization rejected.',flush=True);stop.set();break
                children['erp']=subprocess.Popen([sys.executable,'-u',str(root/'production_agent.py'),'--config',str(args.config)])
            stop.wait(10)
    finally:
        stop.set()
        for child in list(children.values()):terminate(child)


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:pass
