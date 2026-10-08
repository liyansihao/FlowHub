import asyncio
import fcntl
import html
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from flowhub import plugin_pipeline as pipeline
from flowhub.pipeline_modules import ranking_quote, repair_retry
from flowhub.plugin_comparebot import candidate
from flowhub.ranking_sources import CONTRACT


def fixture(sku='3027500170'):
    return json.loads((Path(__file__).parent/'fixtures/ranking-quotes'/f'{sku}.json').read_text())


def packet(f):
    names = {'main': 'webProductMainWidget', 'seller': 'webCurrentSeller', 'price': 'webPrice'}
    return {k: f[k] for k in ('url', 'observed_at')} | {'html': ''.join(
        f'<div id="state-{names[k]}-1" data-state="{html.escape(json.dumps(v), quote=True)}"></div>'
        for k, v in f['widgets'].items())}


@pytest.mark.parametrize('sku', ['3027500170', '2013720657'])
def test_real_page_fixtures_bind_sku_seller_and_ordinary_price(sku):
    f = fixture(sku)
    q = ranking_quote.parse_quote(f, packet(f), f['observed_at']+1)
    assert q['value'] == f['expected_value']
    assert q['currency'] == f['expected_currency']
    assert q['observed_at'] == f['observed_at']
    assert q['seller_id'] == f['seller_id'] and q['sku'] == sku
    assert q['raw'] != f['widgets']['price']['cardPrice'] and q['body_sha256']


@pytest.mark.parametrize('mutation', ['page_sku', 'main_sku', 'price_sku', 'wrong_seller',
    'conflicting_seller', 'missing_seller', 'offsite', 'sold_out', 'card_only', 'old_only',
    'unknown_currency', 'zero', 'stale', 'future', 'duplicate', 'missing_widget'])
def test_unbound_unavailable_ambiguous_or_nonasking_prices_are_rejected(mutation):
    f = fixture(); now = f['observed_at']+1
    w = f['widgets']; price = w['price']
    action = w['seller']['header']['badge']['subscribed']['common']['action']
    if mutation == 'page_sku': f['url'] = 'https://www.ozon.ru/product/999/'
    if mutation == 'main_sku': w['main']['sku'] = '999'
    if mutation == 'price_sku': price['link'] = '/modal/pdpListOfBanks?product_id=999'
    if mutation == 'wrong_seller': action['params']['sellerId'] = '999'
    if mutation == 'conflicting_seller': w['seller']['sellerCell']['common']['action']['link'] = '/seller/wrong-999/'
    if mutation == 'missing_seller': del w['seller']['header']
    if mutation == 'offsite': f['url'] = 'https://example.com/product/3027500170/'
    if mutation == 'sold_out': price['isAvailable'] = False
    if mutation == 'card_only': del price['price']
    if mutation == 'old_only': price['originalPrice'] = price.pop('price')
    if mutation == 'unknown_currency': price['price'] = '12 $'
    if mutation == 'zero': price['price'] = '0 ₽'
    if mutation == 'stale': f['observed_at'] -= 21600
    if mutation == 'future': f['observed_at'] += 10
    if mutation == 'missing_widget': del w['main']
    data = packet(f)
    if mutation == 'duplicate': data['html'] *= 2
    with pytest.raises(ValueError): ranking_quote.parse_quote(f, data, now)


def test_cny_explicit_currency_and_numeric_seller_link():
    f = fixture()
    del f['widgets']['seller']['header']
    f['widgets']['seller']['sellerCell']['common']['action']['link'] = '/seller/store-3471788/'
    f['widgets']['price']['price'] = '25,65 ¥'
    q = ranking_quote.parse_quote(f, packet(f), f['observed_at']+1)
    assert q['currency'] == 'CNY' and q['value'] == 25.65


def configured(tmp_path):
    from tests.test_repair_workflow import configured as base
    db, owner = base(tmp_path, publication=False)
    (tmp_path/'publication-policy.json').write_text('{"backend":"maozi_follow"}')
    (tmp_path/'direct-first.json').write_text('{"enabled":true}')
    (tmp_path/'source-loop.json').write_text(json.dumps({'enabled': True, 'owner': owner, 'profile': str(tmp_path/'browser')}))
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET body=json_set(body,'$.submitted',json('false'))")
        p = json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p.update(coverage='sales-ranking', average_price_rub=999, plugin_detail={},
                 ranking_source={'contract': CONTRACT, 'endpoint': '/api.selection.top/lists',
                    'sku': '1', 'seller_id': '2', 'period': '28d', 'sold_count': 3,
                    'observed_at': time.time(), 'evidence_hash': 'test'})
        c.execute('UPDATE sourcing_products SET body=?', (json.dumps(p),))
    return db, owner


def fake_package(monkeypatch):
    from flowhub.pipeline_modules.repair_reads import RepairReads
    from flowhub.maozi import MaoziPublisher
    from flowhub.source_detail import SourceCollector
    monkeypatch.setattr(RepairReads, 'get', AsyncMock(return_value={
        'sku': '1', 'cate': [1, 2, 3], 'product_info': {'weight': 69, 'depth': 10, 'width': 5, 'height': 2}}))
    # Real sku3 lacks price/seller; the ranking path must not depend on it or create drafts.
    monkeypatch.setattr(MaoziPublisher, 'erp', AsyncMock(side_effect=AssertionError('unexpected sku3')))
    monkeypatch.setattr(SourceCollector, 'collect', AsyncMock(side_effect=AssertionError('unexpected draft')))


async def test_missing_quote_ranking_resumes_original_valuation_and_preserves_provenance(tmp_path, monkeypatch):
    db, owner = configured(tmp_path); fake_package(monkeypatch)
    quote = {'value': 350, 'currency': 'RUB', 'observed_at': time.time(), 'source': ranking_quote.SOURCE}
    read = AsyncMock(return_value=(quote, {'source': ranking_quote.SOURCE, 'fields': ['sale_price'], 'quote': quote}))
    monkeypatch.setattr(ranking_quote, 'read', read)
    assert await pipeline.tick(db, lane='seed_repair', repair_kind='valuation')
    with db.connect() as c:
        p = json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0] == 'queued'
    envelope = candidate(p)
    assert envelope['price'] == 350 and envelope['weight_g'] == 69
    assert envelope['origin']['expansion_source']['contract'] == CONTRACT
    assert p['average_price_rub'] == 999 and p['collected_at'] == 0
    assert 'source_relation' not in p and 'monthly_sales' not in p['plugin_detail']
    assert p['proposed_sale_price'] == quote and read.await_count == 1
    from flowhub.pipeline_modules.repair import PriceRepairModule
    assert (await PriceRepairModule().run_stage(db, owner, '1', '2', stage='facts', purpose='valuation'))['state'] == 'ready'
    assert read.await_count == 1


@pytest.mark.parametrize('reason,failure,expected', [
    ('quote_source_browser_busy', 'remote_pending', 'needs_fields'),
    ('TimeoutError', 'network', 'needs_fields'),
    ('quote_wrong_seller', None, 'needs_review'),
    ('quote_product_unavailable', None, 'needs_review')])
async def test_busy_retries_but_bad_product_evidence_does_not_pass(tmp_path, monkeypatch, reason, failure, expected):
    db, owner = configured(tmp_path); fake_package(monkeypatch)
    step = {'source': ranking_quote.SOURCE, 'fields': [], 'reason': reason}
    if failure: step['failure_class'] = failure
    monkeypatch.setattr(ranking_quote, 'read', AsyncMock(return_value=(None, step)))
    await pipeline.tick(db, lane='seed_repair', repair_kind='valuation')
    with db.connect() as c:
        q = c.execute('SELECT state,body FROM plugin_pipeline').fetchone()
        p = json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
    assert q['state'] == expected
    assert p['plugin_detail']['weight_g'] == 69 and not p.get('proposed_sale_price')
    if failure: assert json.loads(q['body'])['repair_workflow']['failure_class'] == failure


async def test_shared_profile_busy_never_launches_second_browser(tmp_path, monkeypatch):
    db, owner = configured(tmp_path)
    spawn = AsyncMock(side_effect=AssertionError('must not launch'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with (tmp_path/'source-loop.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        quote, step = await ranking_quote.read(db, owner, {'sku': '1'})
    assert quote is None and repair_retry.classify({'steps': [step]}) == 'remote_pending'
    spawn.assert_not_called()


async def test_cancellation_closes_browser_before_releasing_profile(tmp_path, monkeypatch):
    db, owner = configured(tmp_path); entered = asyncio.Event()
    class Process:
        returncode = None
        terminated = False
        async def communicate(self):
            entered.set(); await asyncio.Future()
        def terminate(self): self.terminated = True
        async def wait(self):
            assert self.terminated
            with (tmp_path/'source-loop.lock').open('a') as lock:
                with pytest.raises(BlockingIOError): fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.returncode = 0
    process = Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    task = asyncio.create_task(ranking_quote.read(db, owner, {'sku': '1'}))
    await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert process.returncode == 0
    with (tmp_path/'source-loop.lock').open('a') as lock: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


async def test_storefront_with_existing_quote_never_uses_ranking_browser(tmp_path, monkeypatch):
    db, owner = configured(tmp_path); fake_package(monkeypatch)
    with db.connect() as c:
        p = json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p.update(coverage='storefront-page', source_relation={'seller_id': '2', 'root_seeds': [{'sku': '8'}]},
                 proposed_sale_price={'value': 450, 'currency': 'RUB', 'observed_at': time.time()})
        p.pop('ranking_source')
        c.execute('UPDATE sourcing_products SET body=?', (json.dumps(p),))
    read = AsyncMock(side_effect=AssertionError('storefront must not use ranking browser'))
    monkeypatch.setattr(ranking_quote, 'read', read)
    await pipeline.tick(db, lane='seed_repair', repair_kind='valuation')
    with db.connect() as c: assert c.execute('SELECT state FROM plugin_pipeline').fetchone()[0] == 'queued'
    read.assert_not_called()


@pytest.mark.parametrize('protection', [None, 'human', 'publication', 'lease', 'block'])
async def test_recovery_only_resumes_machine_generated_unreviewed_quote_hold(tmp_path, monkeypatch, protection):
    import runpy
    recover = runpy.run_path(str(Path(__file__).parents[1]/'scripts/recover_ranking_quotes.py'))['recover']
    db, owner = configured(tmp_path); fake_package(monkeypatch)
    monkeypatch.setattr(ranking_quote, 'read', AsyncMock(return_value=(None,
        {'source': ranking_quote.SOURCE, 'reason': 'quote_product_unavailable', 'fields': []})))
    await pipeline.tick(db, lane='seed_repair', repair_kind='valuation')
    with db.connect() as c:
        before = dict(c.execute('SELECT * FROM plugin_pipeline').fetchone())
        if protection == 'human':
            c.execute('CREATE TABLE IF NOT EXISTS human_reviews(owner TEXT,sku TEXT,seller TEXT,body TEXT)')
            c.execute("INSERT INTO human_reviews(owner,sku,seller,body) VALUES(?,'1','2','{}')", (owner,))
        if protection == 'publication':
            c.execute('CREATE TABLE IF NOT EXISTS plugin_publications(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT,updated REAL)')
            c.execute("INSERT INTO plugin_publications(owner,sku,seller,state,body,updated) VALUES(?,'1','2','preparing','{}',0)", (owner,))
        if protection == 'lease':
            c.execute("INSERT INTO plugin_pipeline_leases VALUES(?,'1','2','other',?)", (owner, time.time()+60))
        if protection == 'block':
            c.execute("INSERT INTO blocks(owner,source_key,reason) VALUES(?,'1','human delist')", (owner,))
    plan = recover(db, owner, ['1'])
    assert plan['tasks'][0]['state'] == ('retained' if protection else 'eligible')
    with db.connect() as c: assert dict(c.execute('SELECT * FROM plugin_pipeline').fetchone()) == before
    result = recover(db, owner, ['1'], apply=True)
    with db.connect() as c:
        after = dict(c.execute('SELECT * FROM plugin_pipeline').fetchone())
        if protection:
            assert after == before
        else:
            assert after['state'] == 'needs_fields'
            q = json.loads(after['body']); original = json.loads(before['body'])
            assert q['requested_at'] == original['requested_at'] and q['lifecycle'] == original['lifecycle']
            assert q['repair_workflow']['identity'] == original['repair_workflow']['identity']
            events = [json.loads(r[0]) for r in c.execute('SELECT body FROM repair_workflow_events')]
            audit = next(e for e in events if e.get('event') == 'ranking_quote_recovery')
            assert audit['previous_queue']['body'] == before['body']
    assert result['tasks'][0]['state'] == ('retained' if protection else 'resumed')
    assert recover(db, owner, ['1'], apply=True)['tasks'][0]['state'] == 'retained'
