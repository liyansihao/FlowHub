"""Private stdin/stdout bridge called after the existing global ERP pacing gate."""
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowhub.cluster import Coordinator
from flowhub.cluster_erp import ERPRelay
from flowhub.db import DATA

try:
    relay = ERPRelay(Coordinator(DATA / 'cluster'))
    identity = relay.submit(os.environ['FLOWHUB_ERP_DEVICE'], json.load(sys.stdin),
                            json.loads(os.environ.get('FLOWHUB_ERP_PRODUCT','null')))
    while True:
        result = relay.result(identity)
        if result is not None:
            print(json.dumps(result)); break
        time.sleep(.1)
except Exception:
    # No credentials, request bodies, or provider response in errors.
    print(json.dumps({'error': 'remote_transport_unavailable'}))
