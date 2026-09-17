"""Local-only administration of the multi-PC acceptance coordinator."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.cluster import Coordinator
from flowhub.db import DATA
p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
a=sub.add_parser('invite');a.add_argument('--name',required=True);a.add_argument('--output',type=Path,required=True)
a=sub.add_parser('probe');a.add_argument('--key',required=True)
a=sub.add_parser('revoke');a.add_argument('--device',required=True)
sub.add_parser('status');args=p.parse_args();hub=Coordinator(DATA/'cluster')
if args.action=='invite':
    with args.output.open('x') as f:
        args.output.chmod(0o600);json.dump({'code':hub.invitation(args.name),'expires_in_minutes':30},f)
    print('Enrollment saved to '+str(args.output))
elif args.action=='probe':print(hub.enqueue(args.key))
elif args.action=='revoke':hub.revoke(args.device);print('Device revoked')
else:print(json.dumps(hub.status(),ensure_ascii=False,indent=2))
