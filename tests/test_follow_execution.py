import httpx
import pytest

from flowhub.follow_execution import FollowBatchTransport


@pytest.fixture
def batch():
    calls=[]
    def respond(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(200,json={'code':1,'data':{'data':[], 'total':0}})
    def make(sku='1',account='account'):
        return FollowBatchTransport(httpx.MockTransport(respond),sku=sku,namespace=account)
    return calls,make


async def read(transport, path='/api.product.favorite/lists',sku='1'):
    return await transport.handle_async_request(httpx.Request('GET',
        'https://api.maozierp.com'+path,params={'sku':sku,'is_imported':0}))


async def write(transport):
    return await transport.handle_async_request(httpx.Request('POST',
        'https://api.maozierp.com/api.selection.follow/import',
        json={'rows':[{'sku':transport.sku}]}))


async def test_exact_sku_batch_reused_across_turns_but_not_accounts(batch):
    calls,make=batch
    await read(make());await read(make())
    assert len(calls)==1
    await read(make(account='other'))
    assert len(calls)==2


async def test_other_sku_write_keeps_batch_but_own_write_invalidates(batch):
    calls,make=batch
    a,b=make(),make('2')
    await read(a);await read(b,sku='2')
    await write(a)
    await read(b,sku='2')
    assert len(calls)==3
    await read(make())
    assert len(calls)==4


async def test_expired_absence_cannot_be_reused(batch,monkeypatch):
    calls,make=batch;t=make()
    await read(t)
    from flowhub.pipeline_modules import transport
    now=transport.time.monotonic()
    monkeypatch.setattr(transport.time,'monotonic',lambda:now+61)
    await read(make())
    assert len(calls)==2


async def test_unknown_import_invalidates_prewrite_observation(batch):
    calls,make=batch;t=make();await read(t)
    async def fail(request):raise httpx.ReadTimeout('response lost')
    t.transport=httpx.MockTransport(fail)
    with pytest.raises(httpx.ReadTimeout):await write(t)
    await read(make())
    assert len(calls)==2


async def test_stock_reads_always_fresh(batch):
    calls,make=batch;t=make()
    await read(t,'/api.product.online/get_stock');await read(t,'/api.product.online/get_stock')
    assert len(calls)==2


async def test_auth_failure_discards_all_account_batches(batch):
    calls,make=batch;a,b=make(),make('2')
    await read(a);await read(b,sku='2')
    a.invalidate('/authentication-failure')
    await read(b,sku='2')
    assert len(calls)==3


async def test_shops_shared_across_skus(batch):
    calls,make=batch
    async def shops(t):
        await t.handle_async_request(httpx.Request('GET','https://api.maozierp.com/api.shop/lists'))
    await shops(make());await shops(make('2'))
    assert len(calls)==1


async def test_connection_backoff_remains_shared_by_account(batch):
    calls,make=batch;t=make(account='failing-account')
    async def fail(request):raise httpx.ConnectError('connection failed')
    t.transport=httpx.MockTransport(fail)
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):await read(t)
    from flowhub.pipeline_modules.transport import RemoteReadDeferred
    with pytest.raises(RemoteReadDeferred,match='endpoint recovery'):
        await read(make('2',account='failing-account'),sku='2')
    assert calls==[]
