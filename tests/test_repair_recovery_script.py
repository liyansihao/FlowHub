import json
import time
import pytest
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.manual_reviews import schema
from scripts.recover_repair_regressions import recover
from test_repair_queue import recovery_fixture


@pytest.mark.parametrize('guard',[None,'published','lease','unlist','price_current'])
def test_expired_recovery_resumes_original_request_once_without_renewing_approval(tmp_path,guard):
    db=Database(tmp_path);SourceLibrary(db);schema(db)
    receipt,product,_=recovery_fixture();product['collected_at']=time.time()
    review=json.loads(json.loads(receipt['body'])['review'])
    review.update(state='matched',finished_at=time.time()-(60 if guard=='price_current' else 22000))
    queue={'backend':'official','listing_control_id':'original-list',
           'repair_recovery':{'reason':'single_character_dictionary_validation'}}
    with db.connect() as c:
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,'1','2','needs_fields',json.dumps(queue),0,0))
        c.execute('INSERT INTO plugin_reviews VALUES(?,?,?,?,?,?)',(owner,'1','2','matched',json.dumps(review),0))
        c.execute('INSERT INTO product_listing_controls VALUES(?,?,?,?,?,?,?)',(owner,'1','2','unlist' if guard=='unlist' else 'list','blocked',json.dumps({'id':'original-list'}),0))
        c.execute('INSERT INTO human_reviews VALUES(NULL,?,?,?,?,?,?,?,?,?)',(owner,'1','2',receipt['actor'],'approve',receipt['note'],'revision',receipt['body'],receipt['at']))
        if guard=='published':c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?)',(owner,'1','2','{}'))
        if guard=='lease':c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'1','2','live',time.time()+120))
    SourceLibrary(db).put(owner,product,{'channel':'test'})
    if guard:
        assert recover(db,apply=True)['tasks']==[]
        return
    assert recover(db)['counts']=={'revalue_expired_approval':1}
    assert recover(db,apply=True)['counts']=={'revalue_expired_approval':1}
    assert recover(db,apply=True)['tasks']==[]
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM repair_recovery_receipts').fetchone()[0]==1
        state,body=c.execute('SELECT state,body FROM plugin_reviews').fetchone()
        restored=json.loads(body)
        assert state=='same_product_confirmed'
        assert restored['finished_at']==900 and restored['identity_review']['human_review']['at']==1000
        state,body=c.execute('SELECT state,body FROM product_listing_controls').fetchone()
        assert state=='queued' and json.loads(body)['id']=='original-list'
        assert c.execute('SELECT count(*) FROM plugin_publications').fetchone()[0]==0
