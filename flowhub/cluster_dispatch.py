"""Assign a single active product per Windows device from the existing pipeline.

No second production queue: each assignment selects the transport of the original
Mac operation. Store choice, SKU locks, approval and journal are unchanged.
"""
import json
import sqlite3
import time
from .cluster import Coordinator
from .cluster_erp import ERPRelay


def device_for(directory, policy, key):
    if not policy.get('enabled'):
        return None
    if policy.get('mode') != 'continuous':
        if list(key) in policy.get('products', []):
            if len(policy['products']) != 1:
                raise BlockingIOError('First Windows canary requires exactly one product')
            return policy['device']
        return None
    hub=Coordinator(directory/'cluster');ERPRelay(hub)
    with hub.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS erp_assignments(
            id INTEGER PRIMARY KEY,device TEXT,owner TEXT,sku TEXT,seller TEXT,
            state TEXT,created REAL,finished REAL)''')
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_erp_device ON erp_assignments(device) WHERE state='active'")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_erp_product ON erp_assignments(owner,sku,seller) WHERE state='active'")
        c.execute('BEGIN IMMEDIATE')
        prod=sqlite3.connect(f'file:{directory / "flowhub.sqlite3"}?mode=ro',uri=True,timeout=10)
        try:
            def product(identity):
                return prod.execute('SELECT state,body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',identity).fetchone()
            # Release only settled items. Unknown remote outcomes remain assigned.
            for active in c.execute("SELECT * FROM erp_assignments WHERE state='active'").fetchall():
                current=product((active['owner'],active['sku'],active['seller']))
                if current and current[0] in ('selling','needs_review','failed','rejected'):
                    c.execute('UPDATE erp_assignments SET state=?,finished=? WHERE id=?',
                              (current[0],time.time(),active['id']))
                elif current and json.loads(current[1]).get('phase') in ('reconciling','sync_pending','stock_pending'):
                    # A waiting platform response does not reserve a whole device.
                    # Never hand off the transport during an in-flight pipeline step.
                    has_leases=prod.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_pipeline_leases'").fetchone()
                    busy=has_leases and prod.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',
                        (active['owner'],active['sku'],active['seller'],time.time())).fetchone()
                    command=c.execute("SELECT 1 FROM erp_commands WHERE device=? AND state IN ('queued','claimed','executing') AND deadline>?",
                        (active['device'],time.time())).fetchone()
                    if not busy and not command:
                        c.execute("UPDATE erp_assignments SET state='waiting_on_platform',finished=? WHERE id=?",(time.time(),active['id']))
            mine=c.execute("SELECT device FROM erp_assignments WHERE owner=? AND sku=? AND seller=? AND state='active'",key).fetchone()
            if mine:
                return mine[0]
            current=product(key)
            if not current or current[0]!='publishing':
                return None
            phase=json.loads(current[1]).get('phase')
            if phase not in (None,'','prepared','ready'):
                return None
            for device in policy.get('devices', []):
                if c.execute("SELECT 1 FROM erp_assignments WHERE device=? AND state='active'",(device,)).fetchone():
                    continue
                if not c.execute('''SELECT 1 FROM devices d JOIN erp_devices e ON e.device=d.id
                    WHERE d.id=? AND d.enabled=1 AND e.version=2 AND e.last_seen>?''',(device,time.time()-10)).fetchone():
                    continue
                c.execute('INSERT INTO erp_assignments(device,owner,sku,seller,state,created) VALUES(?,?,?,?,?,?)',
                          (device,*key,'active',time.time()))
                return device
            return None
        finally:
            prod.close()
