import asyncio
import json

import pytest
from test_operations import STORES, Fake, client_for

from ops_console.app import create_app
from ops_console.core import State
from ops_console.handling import Handling, row_key


async def test_internal_handling_persists_filters_and_undo_without_platform_write(tmp_path):
    fake = Fake()
    app = create_app(tmp_path, stores=STORES, api=fake, background=False)
    item = {'id': '1', 'name': 'test', 'offer_id': 'abc', 'sku': '123',
            'present': 0, 'reserved': 0, 'archived': False}
    app.state.state.save('a', 'inventory', [item])
    app.state.state.save('b', 'inventory', [item])
    async with client_for(app) as c:
        rows = (await c.get('/api/inventory?category=zero')).json()['items']
        payload = {'key': rows[0]['handling_key'], 'handled': True, 'version': 0}
        assert (await c.post('/api/handling', json=payload)).status_code == 200
        assert (await c.post('/api/handling', json=payload)).status_code == 409
        assert (await c.get('/api/inventory?category=zero&handled=done')).json()['total'] == 1
        assert (await c.get('/api/inventory?category=zero&handled=pending')).json()['total'] == 1
        persisted = Handling(State(tmp_path), STORES).marks()
        assert persisted[payload['key']]['handled'] is True
        assert (await c.post('/api/handling', json={**payload, 'handled': False, 'version': 1})).status_code == 200
        assert (await c.get('/api/inventory?handled=done')).json()['total'] == 0
        assert (await c.post('/api/handling', json={**payload, 'key': 'unknown'})).status_code == 409
    assert fake.calls == []


async def test_cloud_failure_is_not_recorded_as_success(tmp_path):
    state = State(tmp_path)
    row = {'id': 'x', 'present': 0, 'archived': False}
    state.save('a', 'inventory', [row])
    h = Handling(state, STORES)
    h.config = {'url': 'https://flowhub-review.vercel.app/api/operations', 'token': 'secret'}
    async def fail(*args, **kwargs):
        raise ValueError('network failed')
    h.request = fail
    with pytest.raises(ValueError):
        await h.set(row_key('inventory', 'a', row), True, 0)
    assert h.marks() == {}
    assert 'api_key' not in json.dumps(h.snapshot()['stores'])


async def test_curl_transport_preserves_chinese_russian_and_escapes(tmp_path):
    async def echo(reader, writer):
        headers = await reader.readuntil(b'\r\n\r\n')
        length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                      if line.lower().startswith(b'content-length:'))
        body = await reader.readexactly(length)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                     + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(echo, '127.0.0.1', 0)
    async with server:
        h = Handling(State(tmp_path), STORES)
        h.config = {'url': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/', 'token': 'test'}
        body = {'name': '丽丽三号 Конструктор "Ferrari"', 'note': 'a\\b\nc'}
        assert await h.request('snapshot', body) == body
