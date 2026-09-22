import concurrent.futures
import json
import sqlite3
import time
import pytest
from flowhub.cluster import Coordinator
from flowhub.cluster_erp import ERPRelay
from flowhub.cluster_dispatch import device_for


def setup(tmp_path):
    hub=Coordinator(tmp_path/'cluster');relay=ERPRelay(hub)
    d=hub.enroll(hub.invitation('Windows'),'Windows');relay.claim(d['token'])
    c=sqlite3.connect(tmp_path/'flowhub.sqlite3')
    c.execute('CREATE TABLE plugin_pipeline(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT)')
    c.executemany('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?)',
                  [('o',str(i),'s','publishing','{}') for i in range(3)])
    c.commit();c.close()
    return hub,relay,d,{'enabled':True,'mode':'continuous','devices':[d['device_id']]}


def test_parallel_products_get_one_device_and_terminal_releases(tmp_path):
    hub,relay,d,p=setup(tmp_path)
    keys=[('o','0','s'),('o','1','s')]
    # Initialize schema before the concurrency test.
    device_for(tmp_path,p,('o','nonexistent','s'))
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda key:device_for(tmp_path,p,key),keys))
    assert results.count(d['device_id'])==1
    winner=keys[results.index(d['device_id'])]
    assert device_for(tmp_path,p,winner)==d['device_id']
    assert device_for(tmp_path,p,('o','2','s')) is None
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute("UPDATE plugin_pipeline SET state='selling' WHERE sku=?",(winner[1],))
    assert device_for(tmp_path,p,('o','2','s'))==d['device_id']
    with hub.connect() as c:
        assert c.execute("SELECT count(*) FROM erp_assignments WHERE state='active'").fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM erp_assignments WHERE state='selling'").fetchone()[0]==1


def test_offline_new_work_stays_mac_unknown_work_not_reassigned(tmp_path):
    hub,relay,d,p=setup(tmp_path)
    assert device_for(tmp_path,p,('o','0','s'))==d['device_id']
    with hub.connect() as c:c.execute('UPDATE erp_devices SET last_seen=0')
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute("UPDATE plugin_pipeline SET state='awaiting_remote',body=? WHERE sku='0'",(json.dumps({'phase':'manual_review'}),))
    assert device_for(tmp_path,p,('o','0','s'))==d['device_id']
    assert device_for(tmp_path,p,('o','1','s')) is None
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute("UPDATE plugin_pipeline SET state='needs_review' WHERE sku='0'")
    assert device_for(tmp_path,p,('o','1','s')) is None
    relay.claim(d['token'])
    assert device_for(tmp_path,p,('o','1','s'))==d['device_id']


def test_no_unreviewed_or_already_imported_product_assignment(tmp_path):
    hub,relay,d,p=setup(tmp_path)
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute("UPDATE plugin_pipeline SET state='queued' WHERE sku='0'")
        c.execute("UPDATE plugin_pipeline SET body=? WHERE sku='1'",(json.dumps({'phase':'reconciling'}),))
    assert device_for(tmp_path,p,('o','0','s')) is None
    assert device_for(tmp_path,p,('o','1','s')) is None
    assert device_for(tmp_path,{**p,'enabled':False},('o','2','s')) is None
    assert device_for(tmp_path,p,('o','2','s'))==d['device_id']


def test_platform_wait_releases_device_but_live_step_does_not(tmp_path):
    hub,relay,d,p=setup(tmp_path)
    assert device_for(tmp_path,p,('o','0','s'))==d['device_id']
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute('CREATE TABLE plugin_pipeline_leases(owner TEXT,sku TEXT,seller TEXT,expires REAL)')
        c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?)',('o','0','s',time.time()+60))
        c.execute("UPDATE plugin_pipeline SET body=? WHERE sku='0'",(json.dumps({'phase':'sync_pending'}),))
    assert device_for(tmp_path,p,('o','1','s')) is None
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:c.execute('DELETE FROM plugin_pipeline_leases')
    assert device_for(tmp_path,p,('o','1','s'))==d['device_id']
    with hub.connect() as c:
        assert c.execute("SELECT count(*) FROM erp_assignments WHERE state='waiting_on_platform'").fetchone()[0]==1


def test_new_official_dossier_wait_releases_old_transport_assignment(tmp_path):
    hub,relay,d,p=setup(tmp_path)
    assert device_for(tmp_path,p,('o','0','s'))==d['device_id']
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute("UPDATE plugin_pipeline SET state='needs_fields',body=? WHERE sku='0'",(json.dumps({'phase':'awaiting_dossier','official_dossier_pending':True}),))
    assert device_for(tmp_path,p,('o','1','s'))==d['device_id']


@pytest.mark.parametrize('state',['same_product_confirmed','not_listed','quarantined','delisted'])
def test_parked_product_releases_only_transport_slot(tmp_path,state):
    hub,relay,d,p=setup(tmp_path)
    assert device_for(tmp_path,p,('o','0','s'))==d['device_id']
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute('UPDATE plugin_pipeline SET state=? WHERE sku=?',(state,'0'))
    assert device_for(tmp_path,p,('o','1','s'))==d['device_id']
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        assert c.execute("SELECT state FROM plugin_pipeline WHERE sku='0'").fetchone()[0]==state
    with hub.connect() as c:
        assert c.execute('SELECT count(*) FROM erp_assignments WHERE state=?',(state,)).fetchone()[0]==1


@pytest.mark.parametrize('command_state,method,result,release',[
    ('unknown','POST',None,False),
    ('done','POST',{'error':'remote_outcome_unknown'},False),
    ('done','POST',{'status':200,'body':'not-json'},False),
    ('done','POST',{'status':200,'body':'{"code":1}'},True),
    ('executing','GET',None,False),
    ('unknown','GET',None,True),
])
def test_parked_assignment_preserves_live_or_unknown_writes(tmp_path,command_state,method,result,release):
    hub,relay,d,p=setup(tmp_path)
    device_for(tmp_path,p,('o','0','s'))
    with hub.connect() as c:
        c.execute('INSERT INTO erp_commands VALUES(?,?,?,?,?,?,?,?)',
            ('cmd',d['device_id'],'lease',command_state,None,relay.encode(result) if result else None,time.time(),time.time()+30))
        c.execute('INSERT INTO erp_command_audit VALUES(?,?,?,?)',
            ('cmd',method,'/api.selection.follow/import' if method=='POST' else '/api.shop/lists','["o","0","s"]'))
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute("UPDATE plugin_pipeline SET state='same_product_confirmed' WHERE sku='0'")
    assert device_for(tmp_path,p,('o','1','s'))==(d['device_id'] if release else None)
    with hub.connect() as c:
        assert c.execute('SELECT state FROM erp_commands').fetchone()[0]==command_state


def test_parked_assignment_with_live_lease_stays_owned(tmp_path):
    hub,relay,d,p=setup(tmp_path);device_for(tmp_path,p,('o','0','s'))
    with sqlite3.connect(tmp_path/'flowhub.sqlite3') as c:
        c.execute('CREATE TABLE plugin_pipeline_leases(owner TEXT,sku TEXT,seller TEXT,expires REAL)')
        c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?)',('o','0','s',time.time()+60))
        c.execute("UPDATE plugin_pipeline SET state='same_product_confirmed' WHERE sku='0'")
    assert device_for(tmp_path,p,('o','1','s')) is None
