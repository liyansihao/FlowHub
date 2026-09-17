"""Route one explicitly named production product after Windows v2 is online."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database, DATA
from flowhub.cluster import Coordinator
from flowhub.cluster_erp import ERPRelay

p=argparse.ArgumentParser()
p.add_argument('--device', required=True)
p.add_argument('--owner', required=True)
p.add_argument('--product', action='append', required=True, help='source_sku:seller_id; first canary is one product')
args=p.parse_args()
if len(args.product)!=1:raise SystemExit('First canary must contain exactly one product')
products=[]
for value in args.product:
    sku,seller=value.split(':')
    if not sku.isdigit() or not seller.isdigit():raise SystemExit('Numeric product identity required')
    products.append([args.owner,sku,seller])
hub=Coordinator(DATA/'cluster');ERPRelay(hub)
with hub.connect() as c:
    if not c.execute('''SELECT 1 FROM erp_devices e JOIN devices d ON d.id=e.device
       WHERE d.id=? AND d.enabled=1 AND e.version=2 AND e.last_seen>?''',
       (args.device,time.time()-10)).fetchone():raise SystemExit('Windows production v2 must be online first')
db=Database()
with db.connect() as c:
    for key in products:
        row=c.execute('SELECT state FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        if not row or row['state'] not in ('publishing','awaiting_remote'):
            raise SystemExit('Product must already be in the approved production pipeline')
config=DATA/'cluster/production-routing.json'
if config.exists():raise SystemExit('Existing route must be inspected before replacement')
fd=os.open(config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
    json.dump({'enabled':True,'device':args.device,'products':products,'created':time.time()},f)
print('Exact product routing enabled. Existing production guards and journal remain authoritative.')
