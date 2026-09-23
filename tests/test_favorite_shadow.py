import pytest
from flowhub.pipeline_modules.favorite_shadow import Ledger, inventory


def released():
    return dict(consumer='publication:one',state='released',released_at=10,
                evidence={'receipt':'exact-product'},business_version='v1')


def observe(l, version=1, consumers=None, **kw):
    return l.observe('account','7','123',version,consumers or [released()],
                     business_version='code',now=20,**kw)


def test_defaults_never_release_history(tmp_path):
    l=Ledger(tmp_path/'shadow.db')
    assert observe(l)=='unknown'
    assert not l.candidates(21)


@pytest.mark.parametrize('missing',['coverage_complete','durable_snapshot','independent_import'])
def test_each_missing_proof_protects(tmp_path,missing):
    l=Ledger(tmp_path/'shadow.db');flags=dict(coverage_complete=True,durable_snapshot=True,independent_import=True)
    flags[missing]=False
    assert observe(l,**flags)=='unknown'


def test_release_then_new_dependency_withdraws_and_retains_history(tmp_path):
    l=Ledger(tmp_path/'shadow.db');flags=dict(coverage_complete=True,durable_snapshot=True,independent_import=True)
    assert observe(l,**flags)=='released'
    assert len(l.candidates(21))==1
    assert observe(l,2,[released(),dict(consumer='repair:two',state='required')],**flags)=='required'
    assert not l.candidates(21)
    with l.connect() as c:assert c.execute('SELECT count(*) FROM favorite_dependency_events').fetchone()[0]==2


def test_stale_and_conflicting_events_cannot_restore_candidate(tmp_path):
    l=Ledger(tmp_path/'shadow.db');flags=dict(coverage_complete=True,durable_snapshot=True,independent_import=True)
    observe(l,2,[dict(consumer='repair',state='required')])
    assert observe(l,1,**flags)=='unknown'
    assert not l.candidates(21)
    assert observe(l,2,**flags)=='unknown'
    assert not l.candidates(21)


def test_expiry_and_restart(tmp_path):
    l=Ledger(tmp_path/'shadow.db');flags=dict(coverage_complete=True,durable_snapshot=True,independent_import=True)
    observe(l,**flags)
    assert Ledger(l.path).candidates(21)
    assert not Ledger(l.path).candidates(81)


def test_all_shared_consumers_must_release(tmp_path):
    l=Ledger(tmp_path/'shadow.db')
    assert observe(l,consumers=[released(),dict(consumer='other-shop',state='unknown')],
                   coverage_complete=True,durable_snapshot=True,independent_import=True)=='unknown'


@pytest.mark.asyncio
async def test_inventory_read_interface_and_moving_pages():
    async def get(page):
        return dict(total=101 if page==1 else 100,data=[dict(id=i,sku=str(i)) for i in range(1,101)] if page==1 else [])
    rows,stats=await inventory(get)
    assert len(rows)==100 and not stats['enumeration_consistent']
    assert not stats['authoritative_snapshot']


def test_business_observation_rolls_back_with_caller(tmp_path):
    import sqlite3
    from flowhub.pipeline_modules.favorite_shadow import record_business_state
    c=sqlite3.connect(tmp_path/'business.db')
    c.execute('BEGIN')
    record_business_state(c,'publication:1','ready','v1',favorite_id='7')
    c.commit()
    c.execute('BEGIN')
    record_business_state(c,'publication:1','submitting','v2',favorite_id='7')
    c.rollback()
    assert c.execute('SELECT stage FROM favorite_dependency_observations').fetchall()==[('ready',)]


def test_thousand_candidates_withdraw_without_remote_api(tmp_path):
    l=Ledger(tmp_path/'shadow.db')
    flags=dict(coverage_complete=True,durable_snapshot=True,independent_import=True)
    for i in range(1000):
        l.observe('account',str(i),'123',1,[released()],business_version='code',now=20,**flags)
    assert len(l.candidates(21))==1000
    for i in range(0,1000,2):
        l.observe('account',str(i),'123',2,[dict(consumer='retry',state='required')],business_version='code',now=21)
    assert len(l.candidates(22))==500
    assert not l.candidates(81)


@pytest.mark.asyncio
async def test_invalid_inventory_fails_closed():
    async def get(page):return {'total':1,'data':[{'id':'bad','sku':'123'}]}
    with pytest.raises(ValueError):await inventory(get)
