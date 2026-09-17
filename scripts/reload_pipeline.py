"""Restart this workspace's supervised worker only after draining its lanes."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.pipeline_modules.control import paused,set_paused,schema
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--timeout',type=float,default=180)
a=p.parse_args();db=Database();schema(db)
prior={m:paused(db,m) for m in ('seed','review','publication')};locks=[]
try:
    for m in prior:set_paused(db,m,True)
    deadline=time.monotonic()+a.timeout
    while True:
        with db.connect() as c:
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_pipeline_leases'").fetchone()
            active=c.execute('SELECT COUNT(*) FROM plugin_pipeline_leases WHERE expires>?',(time.time(),)).fetchone()[0] if exists else 0
        if not active:break
        if time.monotonic()>=deadline:raise TimeoutError('worker retained: active pipeline lease')
        time.sleep(.25)
    for name in ('plugin-publication.lock','source-loop.lock','draft-cleanup.lock','favorite-cleanup.lock'):
        lock=(db.directory/name).open('a');locks.append(lock)
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise TimeoutError('worker retained: busy '+name)
                time.sleep(.25)
    pid=json.loads((db.directory/'worker.json').read_text())['pid']
    command=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True).strip()
    expected=str(Path(sys.executable).absolute())+' -m flowhub.worker'
    if command!=expected:raise RuntimeError('worker retained: process identity mismatch')
    os.kill(pid,signal.SIGTERM)
    print(json.dumps({'restart_requested':True,'previous_pid':pid}))
finally:
    for lock in reversed(locks):lock.close()
    for m,v in prior.items():set_paused(db,m,v)
