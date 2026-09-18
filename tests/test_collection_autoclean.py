import json
import pytest
from unittest.mock import AsyncMock
from flowhub.collection_autoclean import tick
from flowhub.collection_reset import maintenance
from flowhub.pipeline_modules import control
from tests.test_draft_cleanup import setup


def configured(tmp_path):
    db,owner,_=setup(tmp_path);control.schema(db)
    (tmp_path/'collection-autoclean.json').write_text(json.dumps({'enabled':True,'threshold_ratio':.85,'target_ratio':0}))
    with db.connect() as c:
        c.execute('INSERT INTO stores(id,owner,name,kind,config,secret) VALUES(?,?,?,?,?,?)',
                  ('test',owner,'test','maozi','{}',db.seal({'erp_token':'test'})))
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize('used,expected',[(849,False),(850,True),(1000,True)])
async def test_threshold_automatically_invokes_archived_maintenance(tmp_path,used,expected):
    db=configured(tmp_path)
    client=AsyncMock();client.call.return_value={'used':used,'limit':1000}
    run=AsyncMock(return_value={'state':'complete','result':{'used':0,'complete':True}})
    result=await tick(db,client_factory=lambda _:client,run_maintenance=run)
    assert run.await_count==int(expected)
    if expected:assert run.call_args.kwargs=={'automatic':True} and result['used_after']==0
    else:assert result['state']=='below_threshold'


@pytest.mark.asyncio
async def test_pause_and_persistent_failure_backoff(tmp_path):
    db=configured(tmp_path);client=AsyncMock();client.call.return_value={'used':900,'limit':1000}
    run=AsyncMock(side_effect=TimeoutError())
    control.set_paused(db,'publication',True)
    assert (await tick(db,client_factory=lambda _:client,run_maintenance=run))['state']=='paused'
    client.call.assert_not_awaited()
    control.set_paused(db,'publication',False)
    assert (await tick(db,client_factory=lambda _:client,run_maintenance=run))['state']=='error'
    await tick(db,client_factory=lambda _:client,run_maintenance=run)
    assert run.await_count==1 and client.call.await_count==1


@pytest.mark.asyncio
async def test_database_lock_retries_only_local_observation(tmp_path,monkeypatch):
    import sqlite3
    from unittest.mock import Mock
    from flowhub import collection_autoclean
    db=configured(tmp_path);client=AsyncMock();client.call.return_value={'used':850,'limit':1000}
    run=AsyncMock(return_value={'state':'complete','result':{'used':0}})
    observe=Mock(side_effect=[sqlite3.OperationalError('database is locked'),None])
    monkeypatch.setattr(collection_autoclean,'observe',observe)
    await tick(db,client_factory=lambda _:client,run_maintenance=run)
    assert observe.call_count==2 and client.call.await_count==1 and run.await_count==1


@pytest.mark.asyncio
async def test_maintenance_rechecks_manual_pause_atomically(tmp_path):
    db=configured(tmp_path);control.set_paused(db,'review',True)
    ctx={'owner':'test','store':{'credentials':{'erp_token':'test'}}}
    assert (await maintenance(db,ctx,automatic=True))['state']=='paused'
    assert control.paused(db,'review') and not control.paused(db,'seed')


def test_monitor_detects_missing_autoclean_timer_and_failure(tmp_path,monkeypatch):
    from flowhub import stability_monitor as monitor
    report={'alerts':[],'captured_at':'test','paused':{},'queue':{},'health':[],
            'first_stock_verified':{'rolling_minutes':{'30':1,'60':1}},'last_collection_capacity_observation':None}
    monkeypatch.setattr(monitor,'audit',lambda _:json.loads(json.dumps(report)))
    (tmp_path/'collection-autoclean.json').write_text('{"enabled":true}')
    events=monitor.sample(tmp_path,tmp_path/'monitor')
    assert any(e['code']=='automatic_collection_cleanup_heartbeat_missing' for e in events)
    (tmp_path/'collection-autoclean-status.json').write_text('{"state":"error","retry_at":9999999999}')
    events=monitor.sample(tmp_path,tmp_path/'monitor')
    assert any(e['code']=='automatic_collection_cleanup_failed' for e in events)
