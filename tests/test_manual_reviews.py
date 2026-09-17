import json,time
import pytest
from fastapi.testclient import TestClient
from flowhub.db import Database,password_hash
from flowhub.api import create_app
from flowhub.manual_reviews import schema,load,revision,decide
from test_plugin_publication import report


def setup(tmp_path):
    db=Database(tmp_path);schema(db)
    from flowhub.source_library import SourceLibrary
    SourceLibrary(db)
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


def test_repair_keeps_audit_and_original_review(tmp_path):
    db,o=setup(tmp_path)
    assert decide(db,o,o,'123','456','repair','补尺寸',rev(db,o))['state']=='needs_fields'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM plugin_reviews').fetchone()[0]==1
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


def review_card(db,owner):
    from flowhub.manual_reviews import card
    with db.connect() as c:return card(c,owner,load(c,owner,'123','456'),'123','456')


@pytest.mark.parametrize('kind',['same_product','missing_fields','stale_evaluation','publication_error'])
def test_cards_classify_work_and_explain_in_chinese(tmp_path,kind):
    db,o=setup(tmp_path)
    with db.connect() as c:
        if kind=='missing_fields':
            c.execute('UPDATE plugin_pipeline SET body=?',(json.dumps({'repair_retry':{'exhausted_at':1,'missing_fields':['attributes','weight_g'],'reason':'missing:attributes,weight_g'}}),))
        if kind=='stale_evaluation':
            r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0]);r['finished_at']=1
            c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
        if kind=='publication_error':c.execute('UPDATE plugin_pipeline SET body=?',(json.dumps({'submitted':True,'reason':'reconciliation_timeout'}),))
    item=review_card(db,o)
    assert item['category']==kind
    assert item['can_approve']==(kind=='same_product')
    assert item['can_repair']==(kind!='publication_error')
    assert any('\u4e00'<=ch<='\u9fff' for ch in item['reason'])
    if kind=='missing_fields':assert '上架商品属性' in item['reason'] and '包装重量' in item['reason']
    if kind=='publication_error':assert item['allowed_actions']==[]


@pytest.mark.parametrize('case',['submitted','publication_record','blocked','active_lease','stale_revision'])
def test_repair_protection_and_card_permissions(tmp_path,case):
    db,o=setup(tmp_path);old=rev(db,o)
    with db.connect() as c:
        if case=='submitted':c.execute('UPDATE plugin_pipeline SET body=?',(json.dumps({'submitted':True}),))
        if case=='publication_record':
            c.execute('CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
            c.execute('INSERT INTO plugin_publications VALUES(?,?,?,?)',(o,'123','456','{}'))
        if case=='blocked':c.execute('INSERT INTO blocks VALUES(?,?,?)',(o,'123','明确下架'))
        if case=='active_lease':c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(o,'123','456','lease',time.time()+60))
        if case=='stale_revision':c.execute('UPDATE plugin_pipeline SET body=?',(json.dumps({'reason':'changed'}),))
    if case!='stale_revision':assert not review_card(db,o)['can_repair']
    with pytest.raises(ValueError):decide(db,o,o,'123','456','repair','补资料',old if case=='stale_revision' else rev(db,o))
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM human_reviews').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM plugin_reviews').fetchone()[0]==1


def test_repair_full_dossier_audit_and_remote_replay(tmp_path):
    db,o=setup(tmp_path)
    retry={'attempts':6,'exhausted_at':time.time(),'missing_fields':['attributes']}
    with db.connect() as c:c.execute('UPDATE plugin_pipeline SET body=?',(json.dumps({'repair_retry':retry}),))
    v=rev(db,o)
    decide(db,o,o,'123','456','repair','补属性后重算',v,replay=True)
    assert decide(db,o,o,'123','456','repair','补属性后重算',v,replay=True)['replayed']
    with db.connect() as c:
        q=json.loads(c.execute('SELECT body FROM plugin_pipeline').fetchone()[0])
        assert q['repair_full_dossier'] is True and q['repair_history']==[retry]
        assert not q.get('submitted') and 'repair_retry' not in q
        rows=c.execute('SELECT body FROM human_reviews').fetchall();assert len(rows)==1
        before=json.loads(rows[0][0]);assert json.loads(before['queue'])['repair_retry']==retry
        assert json.loads(before['review'])['state']=='needs_review'
        assert c.execute('SELECT body FROM plugin_reviews').fetchone()[0]==before['review']
    with pytest.raises(ValueError):decide(db,o,'different','123','456','approve','放行',v,replay=True)


@pytest.mark.parametrize('note',['','   ','x'*1001])
def test_all_entrypoints_require_a_real_note(tmp_path,note):
    db,o=setup(tmp_path)
    with pytest.raises(ValueError,match='审核理由'):decide(db,o,o,'123','456','repair',note,rev(db,o))


def test_export_uses_identical_live_revision_and_includes_missing_reports(tmp_path):
    import importlib.util
    from pathlib import Path
    db,o=setup(tmp_path)
    with db.connect() as c:c.execute("INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,0,0)",(o,'789','456','needs_review','{}'))
    spec=importlib.util.spec_from_file_location('export_reviews',Path(__file__).parents[1]/'netlify-review/scripts/export_reviews.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with db.connect() as c:path=c.execute('PRAGMA database_list').fetchone()[2]
    snapshot=module.export(path,o)
    assert snapshot['total']==2
    assert snapshot['items'][0]==review_card(db,o)
    assert snapshot['items'][1]['category']=='missing_fields'


def test_latest_source_fields_and_all_repair_failures_do_not_override_report(tmp_path):
    from flowhub.source_library import SourceLibrary
    from test_plugin_comparebot import product
    db,o=setup(tmp_path);now=time.time();p=product()
    p.update(sku='123',seller_id='456',collected_at=now)
    p['plugin_detail'].update(observed_at=now,attributes=[{'id':1}])
    p['plugin_detail']['monthly_sales']['observed_at']=now
    p['collection_evidence']={'last_repair':{'at':now,'steps':[
        {'source':'maozi-draft','reason':'SOURCE_REQUEST_FAILED','diagnostic':{'api_message':'采集箱已满，请清理','secret':'must-not-export'}},
        {'source':'maozi-draft','reason':'favorite creation not confirmed; lookup again later'},
        {'source':'maozi-sku3','status':{'update_sales':True}},
    ]}}
    SourceLibrary(db).put(o,p,{'channel':'test'})
    with db.connect() as c:
        r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0]);r['publication_blockers']=['publication_attributes_missing']
        c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
        c.execute('UPDATE plugin_pipeline SET body=?',(json.dumps({'pending_publication_fields':['attributes']}),))
    before=rev(db,o);item=review_card(db,o)
    assert item['missing_fields']==[]
    assert '最新来源资料已补齐' in item['reason']
    assert not item['can_approve'] and '上架商品属性' in item['approval_block']
    failures=item['last_repair']['failures']
    assert len(failures)==3
    assert '容量已满' in failures[0]['reason'] and '尚未确认' in failures[1]['reason'] and '待更新' in failures[2]['reason']
    assert 'must-not-export' not in json.dumps(item)
    assert rev(db,o)==before
    # Attaching a dossier must update the report itself before approval is offered.
    with db.connect() as c:
        r['publication_blockers']=[]
        c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
    assert review_card(db,o)['category']=='same_product'
    assert review_card(db,o)['can_approve']


@pytest.mark.parametrize('original_state',['matched','needs_review'])
def test_repair_retains_report_verbatim_without_renewing_approval(tmp_path,original_state):
    db,o=setup(tmp_path)
    with db.connect() as c:
        r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0]);r['state']=original_state
        raw=json.dumps(r)
        c.execute('UPDATE plugin_reviews SET state=?,body=?',(original_state,raw))
    decide(db,o,o,'123','456','repair','补齐上架商品属性',rev(db,o))
    with db.connect() as c:
        state,body=c.execute('SELECT state,body FROM plugin_reviews').fetchone()
        assert state==original_state and body==raw
        queue=c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
        assert queue[0]=='needs_fields' and json.loads(queue[1])['repair_full_dossier']


@pytest.mark.parametrize('step,expected',[
    ({'source':'maozi-sku3-detail','reason':'no_verified_package_dossier_fields'},None),
    ({'source':'maozi-sku3','status':{'update_sales':True}},'待更新'),
    ({'source':'maozi-sku3','status':{'update_sales':False}},'毛子未返回匹配卖家的有效售价'),
    ({'source':'maozi-sku3','status':{'update_sales':False},'seller_id':'999','price':10},'毛子未返回匹配卖家的有效售价'),
    ({'source':'maozi-sku3','status':{'update_sales':False},'seller_id':'456','price':0},'毛子未返回匹配卖家的有效售价'),
    ({'source':'maozi-sku3','status':{'update_sales':False},'seller_id':'456','price':10},None),
])
def test_sku3_reports_price_failure_not_packaging_note(step,expected):
    from flowhub.manual_reviews import repair_details
    failures=repair_details({'seller_id':'456','collection_evidence':{'last_repair':{'steps':[step]}}})['failures']
    if expected:assert len(failures)==1 and expected in failures[0]['reason']
    else:assert failures==[]


def test_card_does_not_display_authorized_unknown_modes_as_active_failures(tmp_path):
    from flowhub.manual_reviews import card
    db,o=setup(tmp_path)
    with db.connect() as c:
        r=json.loads(c.execute('SELECT body FROM plugin_reviews').fetchone()[0])
        r['publication_blockers']=['fresh_pure_fbs_required','follow_permission_unverified_or_blocked']
        r.setdefault('candidate',{}).setdefault('origin',{}).setdefault('plugin_detail',{})['monthly_sales']={}
        c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
        c.execute('INSERT INTO plugin_publication_permissions(owner,sku,seller,expires,reason) VALUES(?,?,?,?,?)',(o,'123','456',time.time()+60,'existing authorization'))
        item=card(c,o,load(c,o,'123','456'),'123','456')
        assert item['can_approve']
        assert 'fresh_pure_fbs_required' not in item['reason_codes']
        r['candidate']['origin']['plugin_detail']['monthly_sales']['blocked_by_seller']=True
        c.execute('UPDATE plugin_reviews SET body=?',(json.dumps(r),))
        item=card(c,o,load(c,o,'123','456'),'123','456')
        assert not item['can_approve']
        assert 'follow_permission_unverified_or_blocked' in item['reason_codes']
