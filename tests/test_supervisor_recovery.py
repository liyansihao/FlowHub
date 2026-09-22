"""Exercise the real supervisor loop with harmless isolated child processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def test_crashed_worker_restarts_and_supervisor_releases_lock(tmp_path):
    marker=tmp_path/'fake-worker-starts'
    harness=r'''
import os,sys
from flowhub import control
original=control.subprocess.Popen
marker=os.environ['TEST_WORKER_MARKER']
stub="""import os,pathlib,sys,time
p=pathlib.Path(sys.argv[1])
old=p.read_text() if p.exists() else ''
p.write_text(old+str(os.getpid())+'\\n')
if not old:sys.exit(23)
time.sleep(60)
"""
def spawn(command,**kwargs):
    assert command[-2:]==['-m','flowhub.worker']
    return original([sys.executable,'-c',stub,marker],**kwargs)
control.subprocess.Popen=spawn
control.serve()
'''
    env=os.environ|{'FLOWHUB_DATA':str(tmp_path),'FLOWHUB_WORKER_ONLY':'1','TEST_WORKER_MARKER':str(marker)}
    env.pop('FLOWHUB_PORTABLE',None)
    with (tmp_path/'harness.log').open('w') as log:
        supervisor=subprocess.Popen([sys.executable,'-c',harness],env=env,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                rows=marker.read_text().splitlines() if marker.exists() else []
                if len(rows)>=2:break
                assert supervisor.poll() is None,(tmp_path/'harness.log').read_text()
                time.sleep(.1)
            assert len(rows)==2
            assert rows[0]!=rows[1]
            assert json.loads((tmp_path/'worker.json').read_text())['pid']==int(rows[1])
            os.kill(int(rows[1]),0)
        finally:
            supervisor.terminate()
            supervisor.wait(timeout=10)
    import fcntl
    with (tmp_path/'supervisor.lock').open() as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert supervisor.returncode==0
