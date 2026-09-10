import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowhub import production_actions as actions
from test_acceptance import setup, logged


def test_action_auth_scope_csrf_and_values(setup):
    _, admin, user, app = setup
    url = '/api/production/flowef-live99-' + 'a'*20 + '/action'
    payload = dict(action='stock', value=-1, request_id='abcdefghijkl')
    assert logged(app).post(url, json=payload).status_code == 422
    assert logged(app, 'alice', 'Alice-Test-Password-2026').post(url,json=payload).status_code == 403
    assert logged(app).post(url+'?owner='+user,json=payload).status_code == 403
    a=logged(app);a.headers.pop('X-CSRF-Token')
    assert a.post(url,json=payload).status_code == 403


@pytest.mark.parametrize('action,value',[('stock',1.1),('stock',True),('price',float('nan')),('price',-1),('price',1.111),('erase',None)])
def test_bad_values(action,value):
    with pytest.raises(ValueError): actions.validate(action,value)


def test_unknown_stock_write_is_never_replayed(tmp_path,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'FlowEF-production/src'))
    from flowef.adapters.ozon.store_registry import SellerStoreRegistry
    from flowef.adapters.persistence.test_listing_journal import TestListingJournal
    monkeypatch.setattr(actions,'ROOT',tmp_path)
    monkeypatch.setattr(actions,'STATE',tmp_path/'state')
    actions.STATE.mkdir()
    (tmp_path/'flow_b_ef/state').mkdir(parents=True)
    (tmp_path/'flow_b_ef/state/config.json').write_text('{"stores":[]}')
    record=dict(phase='stock_verified',plan=dict(shop_id='104965',warehouse_id='40',sku='123'))
    monkeypatch.setattr(TestListingJournal,'read',lambda self, offer: record)
    calls=[]
    product=SimpleNamespace(product_id='10',sku='20',offer_id='test-offer')
    class Client:
        async def post(self,*a,**kw):
            calls.append(kw['json'])
            raise TimeoutError('unknown outcome')
    class Port:
        client=Client()
        async def find_product(self,*a):return product
        async def read_stocks(self,*a):return [SimpleNamespace(warehouse_id='40',present=3)]
        async def _post(self,*a):return {'items':[dict(id=10,offer_id='test-offer')]}
    async def get(*a):return Port()
    monkeypatch.setattr(SellerStoreRegistry,'get',get)
    request=dict(offer='test-offer',action='delist',request_id='abcdefghijkl')
    first=asyncio.run(actions.operate(request))
    assert first['status']=='pending' and len(calls)==1
    second=asyncio.run(actions.operate(request))
    assert second['status']=='pending' and len(calls)==1
    with pytest.raises(ValueError,match='上次操作'):
        asyncio.run(actions.operate(dict(request,request_id='different-request')))
