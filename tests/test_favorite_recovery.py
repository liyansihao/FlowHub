import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from flowhub.plugin_publication import TestListingJournal as Journal, ZeroStockListingPlan
from flowhub.pipeline_modules.favorite_recovery import recover_favorite
from flowhub.plugin_pipeline import readback_delay
from flowef.application.errors import WriteOutcomeUnknown, RateLimited


def setup(tmp_path, phase='favorite_pending', **details):
    j=Journal(tmp_path/'journal.db')
    p=ZeroStockListingPlan(shop_id='10',sku='20',title='x',cover_image='https://example.com/x.jpg',sell_price_cny='10',watermark_id='30',warehouse_id='40',strategy_version='v1',evidence_report='report',category_id='2',purchase_price_cny='1')
    o=j.prepare(p)
    j.move(o,'prepared',phase,waiting_since=1,**details)
    port=SimpleNamespace(favorite_id=AsyncMock(return_value=None),add_favorite=AsyncMock(),source_has_imports=AsyncMock(return_value=False))
    return j,p,o,port,AsyncMock()


@pytest.mark.asyncio
async def test_late_favorite_recovers_original_offer_without_writes(tmp_path):
    j,p,o,port,guard=setup(tmp_path,'manual_review',reason='reconciliation_timeout',unresolved_phase='favorite_pending')
    port.favorite_id.return_value='50'
    assert await recover_favorite(port,j,o,p,guard,[],now=2000)
    assert j.read(o)['phase']=='ready'
    assert j.read(o)['details']['favorite_id']=='50'
    port.add_favorite.assert_not_called();guard.assert_not_called()


@pytest.mark.asyncio
async def test_acknowledged_missing_favorite_recreated_once_then_read_back(tmp_path):
    j,p,o,port,guard=setup(tmp_path,favorite_attempts=1,favorite_acknowledged_at=1)
    port.favorite_id.side_effect=[None,'50']
    await recover_favorite(port,j,o,p,guard,[],now=200)
    assert j.read(o)['phase']=='ready'
    assert j.read(o)['details']['favorite_attempts']==2
    guard.assert_awaited_once_with(p,o);port.add_favorite.assert_awaited_once_with(p)


@pytest.mark.asyncio
async def test_unknown_retry_is_never_replayed_even_with_legacy_success_event(tmp_path):
    j,p,o,port,guard=setup(tmp_path,favorite_acknowledged_at=1)
    port.add_favorite.side_effect=WriteOutcomeUnknown('lost')
    events=[{'at':1,'from':'prepared','to':'favorite_pending'}]
    with pytest.raises(WriteOutcomeUnknown):await recover_favorite(port,j,o,p,guard,events,now=200)
    await recover_favorite(port,j,o,p,guard,events,now=2000)
    await recover_favorite(port,j,o,p,guard,events,now=4000)
    assert port.add_favorite.await_count==1
    assert j.read(o)['phase']=='manual_review'


@pytest.mark.asyncio
@pytest.mark.parametrize('details,events',[
    ({},[{'at':1,'from':'prepared','error':'WriteOutcomeUnknown'}]),
    ({'favorite_attempts':1},[{'at':1,'from':'prepared','to':'favorite_pending'}]),
])
async def test_no_ack_means_read_only(tmp_path,details,events):
    j,p,o,port,guard=setup(tmp_path,**details)
    await recover_favorite(port,j,o,p,guard,events,now=2000)
    port.add_favorite.assert_not_called();guard.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_ack_is_recoverable_and_attempts_are_capped(tmp_path):
    j,p,o,port,guard=setup(tmp_path,'manual_review',reason='reconciliation_timeout',unresolved_phase='favorite_pending')
    events=[{'at':1,'from':'prepared','to':'favorite_pending'}]
    for now in (200,201,400,600,800):await recover_favorite(port,j,o,p,guard,events,now=now)
    assert port.add_favorite.await_count==2
    assert j.read(o)['details']['reason']=='favorite_visibility_exhausted'


@pytest.mark.asyncio
async def test_imported_source_or_failed_guard_cannot_be_recreated(tmp_path):
    j,p,o,port,guard=setup(tmp_path,favorite_attempts=1,favorite_acknowledged_at=1)
    guard.side_effect=ValueError('explicit_delist')
    with pytest.raises(ValueError):await recover_favorite(port,j,o,p,guard,[],now=200)
    port.add_favorite.assert_not_called()
    guard.side_effect=None;port.source_has_imports.return_value=True
    await recover_favorite(port,j,o,p,guard,[],now=400)
    assert j.read(o)['details']['reason']=='source_already_imported'
    port.add_favorite.assert_not_called()


@pytest.mark.asyncio
async def test_other_timeout_stages_are_untouched(tmp_path):
    j,p,o,port,guard=setup(tmp_path,'manual_review',reason='reconciliation_timeout',unresolved_phase='submitting')
    assert not await recover_favorite(port,j,o,p,guard,[],now=2000)
    port.favorite_id.assert_not_called()
    assert j.read(o)['details']['unresolved_phase']=='submitting'


@pytest.mark.asyncio
async def test_ready_favorite_disappearance_cannot_spin_at_zero_delay(tmp_path):
    j,p,o,port,guard=setup(tmp_path,'ready',favorite_id='50',favorite_attempts=1,favorite_acknowledged_at=1)
    port.favorite_id.side_effect=[None,None]
    assert await recover_favorite(port,j,o,p,guard,[],now=200)
    assert j.read(o)['phase']=='favorite_pending'
    assert readback_delay({},j.read(o)['phase'])==60
    port.add_favorite.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_ready_favorite_keeps_normal_publication_path(tmp_path):
    j,p,o,port,guard=setup(tmp_path,'ready',favorite_id='50')
    port.favorite_id.return_value='50'
    assert not await recover_favorite(port,j,o,p,guard,[],now=200)
    assert j.read(o)['phase']=='ready'
    port.add_favorite.assert_not_called()


def test_favorite_backoff_caps_and_resets_after_progress():
    b={}
    assert [readback_delay(b,'favorite_pending') for _ in range(7)]==[60,120,240,480,600,600,600]
    assert readback_delay(b,'ready')==0
    assert readback_delay(b,'favorite_pending')==60
    assert readback_delay(b,'stock_pending')==20


@pytest.mark.asyncio
async def test_recovered_favorite_reenters_submit_lane(tmp_path,monkeypatch):
    from test_modular_pipeline import setup as pipeline_setup
    from flowhub import plugin_pipeline as pipeline
    db,owner=pipeline_setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='awaiting_remote',body=?",('{"phase":"manual_review"}',))
    advance=AsyncMock(return_value={'phase':'ready','verified':False,'retryable_readback':False})
    monkeypatch.setattr(pipeline,'advance',advance)
    assert await pipeline.tick(db,lane='reconcile_history')
    with db.connect() as c:
        assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0]=='publishing'
    assert await pipeline.tick(db,lane='submit')


@pytest.mark.asyncio
async def test_exhausted_favorite_is_read_only_until_it_reappears(tmp_path):
    j,p,o,port,guard=setup(tmp_path,'manual_review',reason='favorite_visibility_exhausted',unresolved_phase='favorite_pending',favorite_attempts=3,favorite_acknowledged_at=1)
    for now in (200,2000):
        assert await recover_favorite(port,j,o,p,guard,[],now=now)
        assert j.read(o)['phase']=='manual_review'
    port.add_favorite.assert_not_called();guard.assert_not_called()
    port.favorite_id.return_value='late-50'
    assert await recover_favorite(port,j,o,p,guard,[],now=3000)
    assert j.read(o)['phase']=='ready'
    assert j.read(o)['details']['favorite_attempts']==3
    port.add_favorite.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_network_errors_do_not_abandon_unknown_outcome(tmp_path,monkeypatch):
    from test_modular_pipeline import setup as pipeline_setup
    from flowhub import plugin_pipeline as pipeline
    from flowef.application.errors import TemporaryExternalError
    db,_=pipeline_setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='awaiting_remote',body=?,attempts=8",('{"phase":"manual_review","submitted":true}',))
    monkeypatch.setattr(pipeline,'advance',AsyncMock(side_effect=TemporaryExternalError('read timeout')))
    assert await pipeline.tick(db,lane='reconcile_history')
    with db.connect() as c:
        row=c.execute('SELECT state,attempts,due FROM plugin_pipeline').fetchone()
    assert row['state']=='awaiting_remote' and row['attempts']==9
    assert row['due']>time.time()+200


@pytest.mark.asyncio
async def test_exhausted_favorite_waits_one_hour_between_reads(tmp_path,monkeypatch):
    from test_modular_pipeline import setup as pipeline_setup
    from flowhub import plugin_pipeline as pipeline
    db,_=pipeline_setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='publishing',body=?",('{"phase":"manual_review"}',))
    monkeypatch.setattr(pipeline,'advance',AsyncMock(return_value={'phase':'manual_review','verified':False,'retryable_readback':True,'reason':'favorite_visibility_exhausted'}))
    assert await pipeline.tick(db,lane='reconcile_history')
    with db.connect() as c:row=c.execute('SELECT state,due FROM plugin_pipeline').fetchone()
    assert row['state']=='awaiting_remote'
    assert row['due']>time.time()+3590
