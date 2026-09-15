import asyncio
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from comparebot.adapters.alibaba1688.image_search import (
    Alibaba1688ImageSearchAdapter, SearchUnavailable, prepare_search_image,
)
from flowhub.comparebot_process import ScreeningFailure, ScreeningWorker


def test_upload_copy_is_bounded_and_original_unchanged():
    image = Image.effect_noise((3072, 4096), 100).convert('RGB')
    original = io.BytesIO()
    image.save(original, 'PNG')
    raw = original.getvalue()
    encoded = prepare_search_image(raw)
    assert len(encoded) <= 128 * 1024
    with Image.open(io.BytesIO(encoded)) as query:
        assert max(query.size) <= 768
        assert abs(query.width / query.height - .75) < .01
    with Image.open(io.BytesIO(raw)) as unchanged:
        assert unchanged.size == (3072, 4096)


def test_small_upload_not_upscaled():
    data = io.BytesIO()
    Image.new('RGB', (40, 60)).save(data, 'PNG')
    assert Image.open(io.BytesIO(prepare_search_image(data.getvalue()))).size == (40, 60)


@pytest.mark.parametrize('status,expected', [(413, 'upload_http_error'), (200, None)])
def test_sdk_swallowed_upload_failure_is_not_empty_search(monkeypatch, status, expected):
    class Session:
        def __init__(self, **kwargs):
            pass
        def request(self, *args, **kwargs):
            return SimpleNamespace(status_code=status, json=lambda: {'data': {'success': True}})
        def search_by_image(self, path):
            self.request('POST', 'https://example.invalid/?secret=never-log',
                         data={'data': 'imageBase64ToImageId'})
            return []
        def close(self):
            pass
    monkeypatch.setitem(sys.modules, 'search1688api.sync_session', SimpleNamespace(Sync1688Session=Session))
    raw = io.BytesIO()
    Image.new('RGB', (40, 60)).save(raw, 'PNG')
    if expected:
        with pytest.raises(SearchUnavailable) as failure:
            Alibaba1688ImageSearchAdapter()._search(raw.getvalue())
        assert failure.value.diagnostic == {'stage': 'search1688', 'code': expected, 'http_status': 413}
        assert 'secret' not in str(failure.value)
    else:
        assert Alibaba1688ImageSearchAdapter()._search(raw.getvalue()) == ()


def test_recoverable_error_reuses_worker_and_next_request_succeeds():
    async def run():
        worker = ScreeningWorker()
        process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(side_effect=[
                json.dumps({'ok': False, 'reusable': True, 'diagnostic': {'stage': 'search1688', 'code': 'upload_http_error'}}).encode(),
                b'{"ok":true}\n'])))
        worker.process = process
        worker.stop = AsyncMock()
        with pytest.raises(ScreeningFailure):
            await worker.call('rank', {}, '')
        await worker.call('rank', {}, '')
        worker.stop.assert_not_awaited()
        assert worker.process is process
    asyncio.run(run())


def test_broken_worker_protocol_stops_worker():
    async def run():
        worker = ScreeningWorker()
        worker.process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(return_value=b'broken')))
        worker.stop = AsyncMock()
        with pytest.raises(ValueError):
            await worker.call('rank', {}, '')
        worker.stop.assert_awaited_once()
    asyncio.run(run())


def test_warm_empty_candidates_remain_manual_review(monkeypatch):
    from flowhub import comparebot, comparebot_process
    monkeypatch.setenv('FLOWHUB_COMPAREBOT_WARM', '1')
    monkeypatch.setattr(comparebot_process, 'screen', AsyncMock(side_effect=ScreeningFailure(
        {'stage': 'search1688', 'code': 'no_candidates'})))
    result = asyncio.run(comparebot.screen({'source_key': '1', 'title': 'test', 'image': 'https://example.com/a.jpg'}))
    assert result['decision']['outcome'] == 'manual_review'
    assert result['decision']['reason'] == 'no_supplier_candidates'


def test_warm_upload_failure_does_not_become_manual_decision(monkeypatch):
    from flowhub import comparebot, comparebot_process
    monkeypatch.setenv('FLOWHUB_COMPAREBOT_WARM', '1')
    monkeypatch.setattr(comparebot_process, 'screen', AsyncMock(side_effect=ScreeningFailure(
        {'stage': 'search1688', 'code': 'upload_http_error', 'http_status': 413})))
    with pytest.raises(ScreeningFailure):
        asyncio.run(comparebot.screen({'source_key': '1', 'title': 'test', 'image': 'https://example.com/a.jpg'}))


def test_timeout_stops_worker():
    async def run():
        worker = ScreeningWorker()
        worker.process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(side_effect=TimeoutError)))
        worker.stop = AsyncMock()
        with pytest.raises(TimeoutError):
            await worker.call('rank', {}, '')
        worker.stop.assert_awaited_once()
    asyncio.run(run())
