"""Install the localhost-only Mac login service for the acceptance coordinator."""
import os,plistlib,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
label='com.flowhub.coordinator'
folder=Path.home()/'Library/LaunchAgents';folder.mkdir(exist_ok=True)
path=folder/(label+'.plist');logs=root/'data/cluster';logs.mkdir(exist_ok=True,mode=0o700)
job={'Label':label,'ProgramArguments':['/usr/bin/caffeinate','-is',str(root/'.venv/bin/python'),'-m','uvicorn','flowhub.cluster:create_app','--factory','--host','127.0.0.1','--port','38428','--no-access-log','--log-level','warning'],
     'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
     'StandardOutPath':str(logs/'service.log'),'StandardErrorPath':str(logs/'service-error.log')}
if path.exists() and plistlib.loads(path.read_bytes())!=job:
    raise SystemExit('Existing coordinator service differs; inspect it before replacing.')
path.write_bytes(plistlib.dumps(job));path.chmod(0o600)
loaded=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{label}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
if not loaded:subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
print('Coordinator installed on 127.0.0.1:38428; login service with automatic restart.')
