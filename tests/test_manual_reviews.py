import json,time
import pytest
from fastapi.testclient import TestClient
from flowhub.db import Database,password_hash
from flowhub.api import create_app
from flowhub.manual_reviews import schema,load,revision,decide
from test_plugin_publication import report


def setup(tmp_path):
    db=Database(tmp_path);schema(db)
    with db.connect() as c:
        c.execute('UPDATE users SET password=?,must_change=0',(password_hash('Test-Password-2026'),))
        owner=c.execute('SELECT id FROM users').fetchone()[0]
        c.execute("UPDATE workflows SET rules=? WHERE owner=?",(json.dumps({'profit_min':30}),owner))
        r=report();r.update(state='needs_review',finished_at=time.time())
        r['result'].update(manual_review=True,reason='qwen_uncertain_outside_safe_band')
        r['result']['evidence']['source']['comparebot']['decision']={'outcome':'manual_review','qwen_review':{'verdict':'uncertain','reason':'核对形状'}}
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,0,0)',(owner,'123','456','needs_review','{}'))
        c.execute('INSERT INTO plugin_reviews VALUES(?,?,?,?,?,?)',(owner,'123','456','needs_review',json.dumps(r),time.time()))
    return db,owner


def rev(db,owner):
    with db.connect() as c:return revision(load(c,owner,'123','456'))


def test_approval_is_audited_and_double_click_is_rejected(tmp_path):
    db,o=setup(tmp_path);v=rev(db,o)
    assert decide(db,o,'human','123','456','approve','已核对商品本体',v)['state']=='publishing'
    with db.connect() as c:
        r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0])
        assert r['result']['evidence']['source']['automated_decision']['outcome']=='manual_review'
        assert r['result']['evidence']['source']['comparebot']['decision']['outcome']=='approved'
        assert c.execute('SELECT actor FROM human_reviews').fetchone()[0]=='human'
    with pytest.raises(ValueError,match='已变化'):decide(db,o,'human','123','456','approve','再次',v)


@pytest.mark.parametrize('case',['stale','low_profit','brand_conflict','active_lease','blocked','already_submitted'])
def test_approval_retains_business_and_concurrency_gates(tmp_path,case):
    db,o=setup(tmp_path)
    with db.connect() as c:
        r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0])
        if case=='stale':r['finished_at']=1
        if case=='low_profit':r['result']['evidence']['profit']['assessment']['erp_profit_rate_pct']=10
        if case=='brand_conflict':r['result']['evidence']['source']['comparebot']['decision']['qwen_review']['brand_or_model_conflict']=True
        if case=='active_lease':c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(o,'123','456','lease',time.time()+60))
        if case=='blocked':c.execute('INSERT INTO blocks VALUES(?,?,?)',(o,'123','test'))
        if case=='already_submitted':c.execute("UPDATE plugin_pipeline SET body='{\"submitted\":true}'")
        c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
    with pytest.raises(ValueError):decide(db,o,'human','123','456','approve','核对',rev(db,o))
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM human_reviews').fetchone()[0]==0


def test_repair_keeps_audit_but_clears_cached_review(tmp_path):
    db,o=setup(tmp_path)
    assert decide(db,o,o,'123','456','repair','补尺寸',rev(db,o))['state']=='needs_fields'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM plugin_reviews').fetchone()[0]==0
        assert json.loads(c.execute('SELECT body FROM human_reviews').fetchone()[0])['review']


def test_api_requires_auth_csrf_and_owned_queue(tmp_path):
    db,o=setup(tmp_path)
    with TestClient(create_app(db)) as client:
        assert client.get('/api/manual-reviews').status_code==401
        client.post('/api/login',json={'username':'admin','password':'Test-Password-2026'})
        row=client.get('/api/manual-reviews').json()['items'][0]
        payload={'action':'reject','note':'规格不符','revision':row['revision']}
        assert client.post('/api/manual-reviews/123/456',json=payload).status_code==403
        client.headers['X-CSRF-Token']=client.get('/api/me').json()['csrf']
        assert client.post('/api/manual-reviews/123/999',json=payload).status_code==409
        assert client.post('/api/manual-reviews/123/456',json=payload).json()['state']=='rejected'
        assert client.get('/api/manual-reviews').json()['total']==0
        assert client.get('/api/manual-reviews/history').json()[0]['note']=='规格不符'
