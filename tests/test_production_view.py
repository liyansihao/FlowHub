import json
import sqlite3
from datetime import datetime, timezone

from flowhub.production_view import snapshot
from test_acceptance import setup, logged


def test_production_is_admin_only_and_not_another_workspace(setup):
    _, admin, user, app = setup
    assert logged(app, 'alice', 'Alice-Test-Password-2026').get('/api/production').status_code == 403
    assert logged(app).get('/api/production?owner=' + user).json() == {'available': False}


def test_read_only_fresh_updates_and_no_raw_evidence(tmp_path):
    (tmp_path / 'status.json').write_text(json.dumps(dict(active=True, shop_id='1')))
    p = tmp_path / 'production.sqlite3'
    c = sqlite3.connect(p)
    c.execute('CREATE TABLE zero_stock_tests(offer_id,shop_id,sku,plan,phase,updated_at,details)')
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    c.execute('INSERT INTO zero_stock_tests VALUES(?,?,?,?,?,?,?)', ('offer', '1', 'sku', json.dumps(dict(title='test', secret='HIDDEN')), 'stock_pending', now, '{}'))
    c.commit()
    assert snapshot(tmp_path)['overview']['phases'] == {'stock_pending': 1}
    c.execute("UPDATE zero_stock_tests SET phase='stock_verified'")
    c.commit()
    result = snapshot(tmp_path, 'selling')
    assert result['overview']['last_hour'] == 1
    assert result['jobs'][0]['phase'] == 'selling'
    assert 'HIDDEN' not in json.dumps(result)
    assert snapshot(tmp_path, 'attention')['jobs'] == []
    c.close()


def test_capacity_wait_is_visible_without_exposing_credentials(tmp_path):
    import os
    import time

    c = sqlite3.connect(tmp_path / 'production.sqlite3')
    c.execute('CREATE TABLE zero_stock_tests(offer_id,shop_id,sku,plan,phase,updated_at,details)')
    c.close()
    path = tmp_path / 'status.json'
    path.write_text(json.dumps(dict(active=True, admission_state='waiting_for_store_capacity',
        store_quotas={'1': dict(available=True, remaining=0, usage=100, limit=100,
                               reset_at=time.time() + 3600),
                      '2': dict(available=False, error='FileNotFoundError', api_key='HIDDEN')})))
    result = snapshot(tmp_path)
    assert result['active'] and result['waiting']
    assert result['status_label'] == '等待店铺额度或连接'
    assert '100/100' in result['notice']
    assert '1 家缺少 API 凭据' in result['notice']
    assert 'HIDDEN' not in json.dumps(result)
    os.utime(path, (time.time() - 100, time.time() - 100))
    stale = snapshot(tmp_path)
    assert not stale['active'] and not stale['waiting']
    assert stale['status_label'] == '流程未运行或心跳过期'
