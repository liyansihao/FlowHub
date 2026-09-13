import copy
import pytest
from flowhub.plugin_publication import approved, require_quota
from flowhub.plugin_publication import same_postal_package

def test_postal_package_rotation_is_equal_but_size_or_weight_changes_are_not():
    d=dict(package_length=150,package_width=100,package_height=10,package_weight=20)
    p=dict(package_length=1,package_width=15,package_height=10,package_weight=20)
    assert same_postal_package(d,p)
    assert not same_postal_package(d,p|{'package_length':2})
    assert not same_postal_package(d,p|{'package_weight':30})


@pytest.mark.parametrize('total,daily,ok', [('11/1000','100/100',True),('-851/1000','0/100',False),('10/1000','0/100',False),('0/1000','10/100',False),('unknown','10/100',False)])
def test_both_live_capacity_limits_must_have_room(total,daily,ok):
    q={'total':total,'daily_create':daily}
    if ok:require_quota(q)
    else:
        with pytest.raises(ValueError,match='target_quota_unavailable'):require_quota(q)


def report():
    return {'state':'matched','finished_at':100,'result':{'supplier_id':'1','purchase':4.1,'evidence':{
        'source':{'selected_offer_id':'1','selected_cost_cny':4.1,'comparebot':{'decision':{'outcome':'approved'}}},
        'profit':{'assessment':{'erp_profit_rate_pct':47.22}}}}}


def test_exact_approved_module_cost_and_profit():
    r=report();assert approved(r,{'profit_min':30},now=101)==r['result']['evidence']

def test_profit_approval_does_not_override_publication_blockers():
    r=report();r['publication_blockers']=['fresh_pure_fbs_required']
    with pytest.raises(ValueError,match='publication_checks_required'):
        approved(r,{'profit_min':30},now=101)


@pytest.mark.parametrize('case',['stale','pending','threshold','price','offer','review'])
def test_unapproved_evidence_cannot_publish(case):
    r=copy.deepcopy(report())
    if case=='stale':r['finished_at']=-30000
    if case=='pending':r['state']='needs_review'
    if case=='threshold':r['result']['evidence']['profit']['assessment']['erp_profit_rate_pct']=30
    if case=='price':r['result']['purchase']=0.1
    if case=='offer':r['result']['supplier_id']='99'
    if case=='review':r['result']['evidence']['source']['comparebot']['decision']['outcome']='manual_review'
    with pytest.raises(ValueError):approved(r,{'profit_min':30},now=101)


def test_scoped_unknown_permission_only_removes_the_two_unknown_checks():
    r=report();r['publication_blockers']=['fresh_pure_fbs_required','follow_permission_unverified_or_blocked']
    r['candidate']={'origin':{'plugin_detail':{'monthly_sales':{}}}}
    approved(r,{},now=101,allow_unknown=True)
    with pytest.raises(ValueError):approved(r,{},now=101)
    r['publication_blockers'].append('publication_attributes_missing')
    with pytest.raises(ValueError,match='publication_attributes_missing'):approved(r,{},now=101,allow_unknown=True)


@pytest.mark.parametrize('monthly',[{'sales_schema':'FBO'},{'sales_schema':'FBS,FBO'},{'blocked_by_seller':True}])
def test_permission_cannot_override_explicit_restrictions(monthly):
    r=report();r['candidate']={'origin':{'plugin_detail':{'monthly_sales':monthly}}}
    with pytest.raises(ValueError,match='explicit_source_restriction'):approved(r,{},now=101,allow_unknown=True)


def test_live_modes_are_still_checked_with_permission():
    from flowhub.plugin_publication import require_source_modes
    require_source_modes((),{},True,now=101)
    require_source_modes(('FBS',),{},True,now=101)
    for modes,monthly in [(('FBO',),{}),(('FBS','FBO'),{}),((),{'blocked_by_seller':True}),((),{'sales_schema':'FBO'})]:
        with pytest.raises(ValueError):require_source_modes(modes,monthly,True,now=101)
    with pytest.raises(ValueError):require_source_modes((),{},False,now=101)


def test_unknown_permission_never_renews_expired_price():
    r=report();r['candidate']={'origin':{'price_evidence':{'value':40,'currency':'CNY','observed_at':-30000}}}
    with pytest.raises(ValueError,match='price_evidence_stale'):approved(r,{},now=101,allow_unknown=True)
