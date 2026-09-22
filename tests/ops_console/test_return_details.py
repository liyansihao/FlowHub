from test_operations import STORES, Fake

from ops_console.core import APIError, State
from ops_console.return_details import ReturnDetails, classify, reason_cn


def test_cancelled_delivery_timestamp_is_not_buyer_receipt():
    row = {'status': 'cancelled', 'fact_delivery_date': '2026-08-01T10:00:00Z',
           'cancellation': {'cancelled_after_ship': True}}
    assert classify(row, 'Вы не отгрузили заказ вовремя')[0] == 'after_ship'
    assert classify(row, 'Покупатель отказался при вручении: товар не подошел')[0] == 'refused'
    assert classify({'status': 'delivered'}, 'Привезли не тот товар')[0] == 'after_delivery'
    assert classify({'status': 'cancelled', 'cancellation': {'cancelled_after_ship': False}}, '')[0] == 'before_ship'
    assert classify({}, 'Вы не отгрузили заказ вовремя')[0] == 'unknown'
    assert reason_cn('Подделка') == '买家反馈疑似假货'


async def test_detail_reason_cache_and_safe_failure(tmp_path):
    api = Fake([{'returns': {'return_reason': {'name': 'Привезли не тот товар'}, 'comment': 'test'}},
                {'result': {'status': 'delivered', 'customer': {'name': 'must not retain'}}}])
    details = ReturnDetails(State(tmp_path), api)
    row = {'id': '123', 'scheme': 'rFBS', 'order': 'order', 'reason': ''}
    result = await details.enrich(STORES[0], row)
    assert result['reason'] == 'Привезли не тот товар'
    assert result['reason_cn'] == '收到的商品不对／发错货'
    assert result['return_stage'] == 'after_delivery'
    assert await details.enrich(STORES[0], row) == result
    assert len(api.calls) == 2
    with details.state.connect() as c:
        assert 'must not retain' not in str([dict(r) for r in c.execute('SELECT * FROM return_details')])
    api.results = [APIError('permission denied'), APIError('permission denied')]
    failed = await details.enrich(STORES[1], row)
    assert failed['return_stage'] == 'unknown' and failed['detail_error']
    assert failed['reason'] == ''
