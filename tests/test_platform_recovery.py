from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from flowhub.plugin_publication import TestListingJournal as Journal, ZeroStockListingPlan
from flowhub.pipeline_modules.platform_recovery import recover_platform_warning
from flowef.application.ports.test_listing import ListedProduct


def setup(tmp_path):
    j=Journal(tmp_path/'journal.db')
    p=ZeroStockListingPlan('10','20','x','https://example.com/x.jpg','10','30','40','v1','report','2','1')
    o=j.prepare(p);j.move(o,'prepared','manual_review',reason='platform_issue_requires_review')
    product=ListedProduct('60','70','10',o,'80','ready_to_sell',0,('pics_http_error',),
        platform_issues=({'code':'pics_http_error','level':'ERROR_LEVEL_WARNING'},),primary_image='https://img.example/ok.jpg')
    return j,p,o,SimpleNamespace(find_product=AsyncMock(return_value=product)),AsyncMock()


@pytest.mark.asyncio
async def test_warning_recovery_keeps_offer_and_runs_preflight(tmp_path):
    j,p,o,port,guard=setup(tmp_path)
    assert await recover_platform_warning(port,j,o,p,guard)
    guard.assert_awaited_once_with(p,o)
    assert j.read(o)['phase']=='reconciling'
    assert j.read(o)['plan']==p.to_dict()
    assert not await recover_platform_warning(port,j,o,p,guard)
    assert port.find_product.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['explicit_delist','hour_window_closed','fresh_comparebot_approval_required'])
async def test_warning_does_not_override_guard(tmp_path,reason):
    j,p,o,port,guard=setup(tmp_path);guard.side_effect=ValueError(reason)
    with pytest.raises(ValueError,match=reason):await recover_platform_warning(port,j,o,p,guard)
    assert j.read(o)['phase']=='manual_review'


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['error','identity','missing','image','inactive'])
async def test_recovery_uses_fresh_exact_observation(tmp_path,case):
    j,p,o,port,guard=setup(tmp_path);product=port.find_product.return_value
    if case=='error':product=replace(product,platform_issues=({'code':'pics_http_error','level':'ERROR_LEVEL_ERROR'},))
    if case=='identity':product=replace(product,offer_id='other')
    if case=='missing':product=None
    if case=='image':product=replace(product,primary_image='')
    if case=='inactive':product=replace(product,status='not_selling')
    port.find_product.return_value=product
    assert not await recover_platform_warning(port,j,o,p,guard)
    assert j.read(o)['phase']=='manual_review'
    guard.assert_not_awaited()
