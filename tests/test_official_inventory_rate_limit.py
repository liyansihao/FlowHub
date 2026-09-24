import copy
import json
import time
from types import SimpleNamespace

import pytest
from flowef.adapters.ozon.seller_inventory import OzonSellerInventoryAdapter
from flowef.application.errors import ExternalContractError, RateLimited

from flowhub.official_inventory import ScheduledInventoryAdapter, explicit_stock_rate_limit
from flowhub import plugin_pipeline
from flowhub.db import Database
from flowhub.source_library import SourceLibrary


PRODUCT = SimpleNamespace(offer_id='offer-1', product_id='123')


def record():
    return {'phase': 'stock_pending', 'details': {
        'stock_write_transport': 'ozon-seller-api', 'stock_http_status': 200,
        'stock_request': {'stocks': [{'offer_id': 'offer-1', 'product_id': 123,
                                      'warehouse_id': 456, 'stock': 99}]},
        'stock_response': {'result': [{'offer_id': 'offer-1', 'product_id': 0,
                                       'warehouse_id': 456, 'updated': False,
                                       'errors': [{'code': 'TOO_MANY_REQUESTS'}]}]},
    }}


def test_only_exact_journaled_rejection_is_rate_limited():
    good = record()
    assert explicit_stock_rate_limit(good, PRODUCT, '456', 99)
    for change in (
        lambda r: r.update(phase='manual_review'),
        lambda r: r['details'].update(stock_http_status=500),
        lambda r: r['details']['stock_request']['stocks'][0].update(stock=0),
        lambda r: r['details']['stock_request']['stocks'][0].update(product_id=999),
        lambda r: r['details']['stock_response']['result'][0].update(offer_id='other'),
        lambda r: r['details']['stock_response']['result'][0].update(updated=True),
        lambda r: r['details']['stock_response']['result'][0].update(
            errors=[{'code': 'TOO_MANY_REQUESTS'}, {'code': 'OTHER'}]),
    ):
        bad = copy.deepcopy(good)
        change(bad)
        assert not explicit_stock_rate_limit(bad, PRODUCT, '456', 99)


@pytest.mark.asyncio
async def test_inventory_adapter_classifies_only_exact_rejection(monkeypatch):
    async def rejected(*args):
        raise ExternalContractError('Seller inventory acknowledgement rejected or mismatched')
    monkeypatch.setattr(OzonSellerInventoryAdapter, 'set_stocks', rejected)
    adapter = ScheduledInventoryAdapter.__new__(ScheduledInventoryAdapter)
    adapter.warehouse_id = '456'
    adapter.journal = SimpleNamespace(read=lambda offer: record())
    with pytest.raises(RateLimited):
        await adapter.set_stocks(PRODUCT, {'456'}, 99)
    adapter.journal = SimpleNamespace(read=lambda offer: {'phase': 'stock_pending', 'details': {}})
    with pytest.raises(ExternalContractError):
        await adapter.set_stocks(PRODUCT, {'456'}, 99)


@pytest.mark.asyncio
async def test_publication_rate_limit_keeps_original_intent_with_backoff(tmp_path, monkeypatch):
    db = Database(tmp_path)
    SourceLibrary(db)
    plugin_pipeline.schema(db)
    with db.connect() as c:
        owner = c.execute('SELECT id FROM users').fetchone()[0]
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',
                  (owner, '1', '2', 'publishing', json.dumps({'phase': 'reconciling',
                                                             'offer_id': 'original', 'submitted': True}), 0, 0))
    async def limited(*args):
        raise RateLimited(60)
    monkeypatch.setattr(plugin_pipeline, 'advance', limited)
    assert await plugin_pipeline.tick(db, lane='reconcile')
    with db.connect() as c:
        row = c.execute('SELECT state,due,attempts,body FROM plugin_pipeline').fetchone()
    assert row['state'] == 'publishing'
    assert row['due'] - time.time() >= 55
    assert row['attempts'] == 1
    assert json.loads(row['body'])['offer_id'] == 'original'
