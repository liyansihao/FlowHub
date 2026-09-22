import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from ops_console.app import create_app
from ops_console.core import APIError, Monitor, Ozon, State, inventory_class

STORES = [{'id': 'a', 'name': '店A', 'client_id': '1', 'api_key': 'secret-test-a'},
          {'id': 'b', 'name': '店B', 'client_id': '2', 'api_key': 'secret-test-b'}]
CHAT = {'id': 'buyer', 'type': 'BUYER_SELLER', 'status': 'OPENED', 'unread': 2,
        'last_message_id': '100', 'created_at': '2026-09-19T00:00:00Z'}


class Fake:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    async def call(self, store, path, body, **kwargs):
        self.calls.append((store['id'], path, body, kwargs))
        if self.results:
            r = self.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return {'result': {'message_id': 123}}


def monitor(tmp_path, fake=None):
    state = State(tmp_path)
    state.save('a', 'chats', [CHAT, {**CHAT, 'id': 'system', 'type': 'SELLER_SUPPORT'}])
    state.save('b', 'chats', [{**CHAT, 'id': 'other'}])
    return Monitor(state, STORES, fake or Fake())


@asynccontextmanager
async def client_for(app):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver')
    await c.get('/')
    bootstrap = (await c.get('/api/bootstrap')).json()
    c.headers['X-Ops-CSRF'] = bootstrap['csrf']
    try:
        yield c
    finally:
        await c.aclose()


def test_inventory_unknown_archived_and_strict_threshold():
    assert inventory_class({'present': None, 'archived': False}, 5) == 'unknown'
    assert inventory_class({'present': 0, 'archived': True}, 5) == 'archived'
    assert inventory_class({'present': 0}, 5) == 'zero'
    assert inventory_class({'present': 4}, 5) == 'low'
    assert inventory_class({'present': 5}, 5) == 'normal'


async def test_send_once_even_if_same_request_repeated_concurrently(tmp_path):
    api = Fake()
    m = monitor(tmp_path, api)
    a, b = await asyncio.gather(m.send('a', 'buyer', '您好', 'request-000000001'),
                                m.send('a', 'buyer', '您好', 'request-000000001'))
    assert a['status'] == b['status'] == 'sent'
    assert len(api.calls) == 1
    assert api.calls[0][0] == 'a'
    assert api.calls[0][3] == {'send': True}


async def test_reject_wrong_shop_system_and_conflicting_request(tmp_path):
    api = Fake()
    m = monitor(tmp_path, api)
    for store, chat in [('b', 'buyer'), ('a', 'system')]:
        with pytest.raises(ValueError):
            await m.send(store, chat, 'test', 'request-000000001')
    assert not api.calls
    await m.send('a', 'buyer', 'one', 'request-000000001')
    with pytest.raises(ValueError):
        await m.send('a', 'buyer', 'changed', 'request-000000001')
    assert len(api.calls) == 1


async def test_timeout_persists_unknown_and_prevents_new_retry(tmp_path):
    api = Fake([APIError('timeout', uncertain=True)])
    m = monitor(tmp_path, api)
    result = await m.send('a', 'buyer', 'test', 'request-000000001')
    assert result['status'] == 'uncertain'
    assert (await m.send('a', 'buyer', 'test', 'request-000000001'))['status'] == 'uncertain'
    with pytest.raises(ValueError):
        await m.send('a', 'buyer', 'test', 'request-000000002')
    assert len(api.calls) == 1
    restarted = Monitor(State(tmp_path), STORES, Fake())
    with pytest.raises(ValueError):
        await restarted.send('a', 'buyer', 'test', 'request-000000003')


async def test_invalid_success_cannot_claim_message_sent(tmp_path):
    m = monitor(tmp_path, Fake([{'result': {}}]))
    assert (await m.send('a', 'buyer', 'test', 'request-000000001'))['status'] == 'uncertain'


async def test_stale_data_is_not_erased_by_failed_sync(tmp_path):
    m = monitor(tmp_path, Fake([APIError('权限不足')]))
    m.state.save('a', 'inventory', [{'id': 'old', 'present': 0}])
    previous = m.state.get('a', 'inventory')['success']
    await m.collect(STORES[0], 'inventory')
    snap = m.state.get('a', 'inventory')
    assert snap['body'][0]['id'] == 'old'
    assert snap['success'] == previous
    assert snap['error'] == '权限不足'


def test_event_dedupe_buyer_only_and_restart_watermark(tmp_path):
    state = State(tmp_path)
    state.chat_events('a', [CHAT, {**CHAT, 'id': 'system', 'type': 'SELLER_SUPPORT', 'unread': 999}])
    state.chat_events('a', [CHAT])
    state = State(tmp_path)
    state.chat_events('a', [CHAT])
    state.chat_events('a', [{**CHAT, 'last_message_id': '101', 'unread': 3}])
    with state.connect() as c:
        events = list(c.execute('SELECT * FROM events'))
    assert len(events) == 2
    assert all(e['chat'] == 'buyer' for e in events)


async def test_chat_pagination_and_loop_protection(tmp_path):
    def page(chat_id, cursor, more):
        return {'chats': [{'chat': {'chat_id': chat_id, 'chat_type': 'BUYER_SELLER'}, 'unread_count': 0}],
                'has_next': more, 'cursor': cursor}
    api = Fake([page('1', 'next', True), page('2', 'done', False)])
    m = monitor(tmp_path, api)
    assert len(await m.fetch_chats(STORES[0])) == 2
    assert api.calls[1][2]['cursor'] == 'next'
    m.api = Fake([page('1', 'same', True), page('2', 'same', True)])
    with pytest.raises(APIError):
        await m.fetch_chats(STORES[0])


async def test_stock_cursor_missing_does_not_publish_partial_snapshot(tmp_path):
    m = monitor(tmp_path, Fake([{'items': [{'product_id': 1, 'stocks': []}], 'total': 20, 'cursor': ''}]))
    with pytest.raises(APIError):
        await m.fetch_inventory(STORES[0])


async def test_only_authorized_transport_paths():
    api = Ozon()
    for path in ('/v2/products/stocks', '/v1/returns/rfbs/action/set', '/v1/chat/send/message'):
        with pytest.raises(ValueError):
            await api.call(STORES[0], path, {})


async def test_api_auth_csrf_no_secrets_and_stale_visibility(tmp_path):
    app = create_app(tmp_path, stores=STORES, api=Fake(), background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
        assert (await c.get('/api/overview')).status_code == 401
        await c.get('/')
        assert (await c.post('/api/refresh')).status_code == 403
        bootstrap = (await c.get('/api/bootstrap')).json()
        c.headers['X-Ops-CSRF'] = bootstrap['csrf']
        assert (await c.post('/api/stores/a/settings', json={'threshold': 4}, headers={'Origin': 'http://evil.example'})).status_code == 403
        assert (await c.get('/api/bootstrap', headers={'Host': 'evil.example'})).status_code == 403
        assert (await c.post('/api/stores/a/settings', json={'threshold': 4})).status_code == 200
        overview = (await c.get('/api/overview')).json()
        assert all(s['channels']['inventory']['stale'] for s in overview['stores'])
        assert 'api_key' not in json.dumps(bootstrap) + json.dumps(overview)
        assert 'secret-test' not in json.dumps(bootstrap) + json.dumps(overview)


async def test_mute_and_threshold_are_local_only_and_csv_safe(tmp_path):
    api = Fake()
    app = create_app(tmp_path, stores=STORES, api=api, background=False)
    app.state.state.save('a', 'inventory', [{'id': '1', 'name': '=HYPERLINK("bad")', 'offer_id': '+123',
        'sku': '42', 'present': 4, 'reserved': 0, 'archived': False}])
    async with client_for(app) as c:
        assert (await c.get('/api/inventory')).json()['total'] == 1
        assert "'=HYPERLINK" in (await c.get('/api/inventory/export')).text
        assert (await c.post('/api/stores/a/mute', json={'product': '1', 'days': 7})).status_code == 200
        assert (await c.get('/api/inventory')).json()['total'] == 0
        assert (await c.get('/api/inventory?category=muted')).json()['total'] == 1
        await c.post('/api/stores/a/mute', json={'product': '1', 'days': 0})
        await c.post('/api/stores/a/settings', json={'threshold': 4})
        assert (await c.get('/api/inventory')).json()['total'] == 0
        assert (await c.get('/api/inventory?category=all')).json()['total'] == 1
    assert not api.calls


async def test_send_http_route_and_history_preserve_shop_binding(tmp_path):
    api = Fake([{'result': {'message_id': 123}}, {'messages': [], 'has_next': False}])
    app = create_app(tmp_path, stores=STORES, api=api, background=False)
    app.state.state.save('a', 'chats', [CHAT])
    async with client_for(app) as c:
        assert (await c.post('/api/chats/b/buyer/send', json={'text': 'test', 'request_id': 'request-000000001'})).status_code == 409
        r = await c.post('/api/chats/a/buyer/send', json={'text': 'test', 'request_id': 'request-000000001'})
        assert r.json()['status'] == 'sent'
        history = await c.get('/api/chats/a/buyer/history')
        assert history.json()['outbox'][0]['body'] == 'test'
    assert api.calls[0][0] == 'a'


async def test_explicit_reconciliation_is_local_and_allows_new_attempt(tmp_path):
    api = Fake([APIError('timeout', uncertain=True), {'result': {'message_id': 124}}])
    app = create_app(tmp_path, stores=STORES, api=api, background=False)
    app.state.state.save('a', 'chats', [CHAT])
    async with client_for(app) as c:
        first = {'text': 'test', 'request_id': 'request-000000001'}
        assert (await c.post('/api/chats/a/buyer/send', json=first)).json()['status'] == 'uncertain'
        assert (await c.post('/api/outbox/request-000000001/resolve',
                             json={'outcome': 'confirmed_not_sent'})).status_code == 200
        assert len(api.calls) == 1
        assert (await c.post('/api/chats/a/buyer/send',
                             json={'text': 'test', 'request_id': 'request-000000002'})).json()['status'] == 'sent'
        assert len(api.calls) == 2


def test_process_restart_turns_inflight_write_into_uncertain(tmp_path):
    state = State(tmp_path)
    with state.connect() as c:
        c.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?)',
                  ('req', 'a', 'buyer', 'test', 'sending', '', '', 1))
    state = State(tmp_path)
    with state.connect() as c:
        assert c.execute('SELECT status FROM outbox').fetchone()[0] == 'uncertain'
