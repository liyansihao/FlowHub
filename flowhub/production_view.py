"""Sanitized read-only view of the co-located production publisher."""
import hashlib
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PHASES = {'stock_verified': 'selling', 'manual_review': 'attention', 'failed': 'attention',
          'favorite_pending': 'prepared', 'submitting': 'publishing'}


def snapshot(directory, phase=''):
    directory = Path(directory).resolve()
    path = directory / 'production.sqlite3'
    if not path.exists():
        return {'available': False}
    status = json.loads((directory / 'status.json').read_text())
    heartbeat = (directory / 'status.json').stat().st_mtime
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute('SELECT offer_id,shop_id,sku,plan,phase,updated_at,details FROM zero_stock_tests ORDER BY updated_at DESC').fetchall()
    names = {}
    config = directory.parents[2] / 'flow_b_ef/state/config.json'
    if config.is_file():
        try:
            names = {str(t['store_id']): t['store_name'] for t in json.loads(config.read_text()).get('stores', [])}
        except (OSError, ValueError, KeyError):
            pass
    counts = Counter()
    recent = 0
    jobs = []
    for r in rows:
        updated = datetime.fromisoformat(r['updated_at']).replace(tzinfo=timezone.utc).timestamp()
        mapped = PHASES.get(r['phase'], r['phase'])
        action_file = directory / 'management' / (hashlib.sha256(r['offer_id'].encode()).hexdigest() + '.json')
        management = json.loads(action_file.read_text()) if action_file.exists() else {}
        if management.get('status') == 'verified':
            if management.get('action') == 'archive':
                mapped = 'archived'
            elif management.get('observed_stock') == 0:
                mapped = 'offline'
            elif management.get('observed_stock', 0) > 0:
                mapped = 'selling'
        if management.get('status') == 'pending':
            mapped = 'attention'
        counts[mapped] += 1
        recent += int(mapped == 'selling' and updated > time.time() - 3600)
        if (phase and mapped != phase) or len(jobs) >= 100:
            continue
        plan = json.loads(r['plan'])
        proof = {}
        evidence = Path(plan.get('evidence_report') or '/missing').resolve()
        if evidence.parent == directory and evidence.is_file():
            try:
                proof = json.loads(evidence.read_text())
            except (OSError, ValueError):
                pass
        source = proof.get('source', {})
        image = source.get('selected_offer_image') or {}
        jobs.append(dict(id=r['offer_id'], source_key=r['sku'], phase=mapped,
                         store_id=r['shop_id'], store_name=names.get(r['shop_id'], '店铺 ' + r['shop_id']), title=plan.get('title', ''),
                         image=plan.get('cover_image'), supplier_image=source.get('selected_image_url'),
                         supplier_url=source.get('selected_offer_url'), score=image.get('score'),
                         dhash=image.get('dhash_score'), stock=plan.get('stock_target', 99), live=True,
                         profit=None if management.get('observed_price') is not None else proof.get('profit', {}).get('assessment', {}).get('erp_profit_rate_pct'),
                         note=management.get('message') or ({'unsupported_activation_status': '平台尚未确认可售，可选择继续上架或下架处理'}.get(json.loads(r['details'] or '{}').get('reason'), '正式上架记录')),
                         management_status=management.get('status'), can_manage=r['phase'] in {'manual_review', 'stock_verified'},
                         price=management.get('observed_price', float(plan.get('sell_price_cny') or 0)),
                         observed_stock=management.get('observed_stock'),
                         updated=max(updated, management.get('updated', 0)), created=updated))
    active = bool(status.get('active') and time.time() - heartbeat < 90)
    quotas = []
    for shop, row in status.get('store_quotas', {}).items():
        available = row.get('available') is True
        quotas.append(dict(store_id=shop, store_name=names.get(shop, shop),
                           available=available, remaining=row.get('remaining', 0),
                           limit=row.get('limit'), usage=row.get('usage'),
                           reset_at=row.get('reset_at'),
                           reason='' if available else ('缺少店铺 API 凭据' if row.get('error') == 'FileNotFoundError' else '店铺连接核验失败')))
    waiting = active and status.get('admission_state') == 'waiting_for_store_capacity'
    label = '等待店铺额度或连接' if waiting else '正在运行' if active else '流程未运行或心跳过期'
    notice = ''
    if waiting:
        exhausted = [q for q in quotas if q['available'] and q['remaining'] == 0]
        unavailable = [q for q in quotas if not q['available']]
        details = [f"{q['store_name']} 今日创建额度 {q['usage']}/{q['limit']}" for q in exhausted]
        if unavailable:
            missing = sum(q['reason'] == '缺少店铺 API 凭据' for q in unavailable)
            details.append(f"{len(unavailable)} 家店铺连接不可用（其中 {missing} 家缺少 API 凭据）")
        resets = [q['reset_at'] for q in exhausted if q['reset_at'] and q['reset_at'] > time.time()]
        if resets:
            notice = '；'.join(details) + '。额度恢复后自动重试，已提交任务继续回查。'
        else:
            notice = '；'.join(details) + '。正在定期重试，已提交任务继续回查。'
    return dict(available=True, jobs=jobs, active=active, shop_id=status.get('shop_id'), shop_name=names.get(str(status.get('shop_id')), str(status.get('shop_id'))),
                status_label=label, notice=notice, waiting=waiting,
                fetched_at=time.time(), overview=dict(phases=dict(counts), last_hour=recent,
                worker_alive=active, heartbeat=heartbeat, quotas=quotas, uptime=0))
