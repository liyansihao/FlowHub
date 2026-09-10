"""Explicit administrator operations on terminal, journal-owned production products.

Runs under the existing FlowEF Python environment. An uncertain write is never replayed:
the same request or the verify action only reads back the persisted intent.
"""
import asyncio
import fcntl
import hashlib
import json
import math
import os
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'FlowEF-production/state/production'


def validate(action, value):
    if action not in {'activate', 'delist', 'archive', 'stock', 'price', 'verify'}:
        raise ValueError('不支持的操作')
    if action == 'stock' and (type(value) is not int or not 0 <= value <= 10000):
        raise ValueError('库存须为0至10000的整数')
    if action == 'price' and (type(value) not in (int, float) or not math.isfinite(value)
                              or not 0.01 <= value <= 1000000 or round(value, 2) != value):
        raise ValueError('价格须大于0，最多两位小数')


def action_path(offer):
    return STATE / 'management' / (hashlib.sha256(offer.encode()).hexdigest() + '.json')


def save(path, data):
    data['updated'] = time.time()
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False))
    temp.chmod(0o600)
    temp.replace(path)


async def operate(req):
    validate(req['action'], req.get('value'))
    sys.path.insert(0, str(ROOT / 'FlowEF-production/src'))
    import httpx
    from flowef.adapters.erp.flowb_bridge import FlowBBridge, FlowBHttpTransport
    from flowef.adapters.erp.maozi_test_listing import MaoziZeroStockAdapter
    from flowef.adapters.ozon.store_registry import SellerStoreRegistry
    from flowef.adapters.persistence.test_listing_journal import TestListingJournal
    from flowef.adapters.persistence.production_blocks import ProductionBlocks
    from flowef.adapters.feishu.delist_feedback import FeishuDelistFeedback
    from contextlib import AsyncExitStack

    offer = req['offer']
    journal = TestListingJournal(STATE / 'production.sqlite3')
    record = journal.read(offer)
    if record['phase'] not in {'manual_review', 'stock_verified'}:
        raise ValueError('商品仍在自动处理中，请等待回查完成后操作')
    plan = record['plan']
    path = action_path(offer)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    lock = (path.parent / 'actions.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError('已有商品操作正在执行，请稍后重试')
    prior = json.loads(path.read_text()) if path.exists() else None
    verifying = req['action'] == 'verify' or bool(prior and prior.get('request_id') == req['request_id'])
    if req['action'] == 'verify' and not prior:
        raise ValueError('尚无操作记录，请先选择要执行的操作')
    if prior and prior['status'] == 'pending' and not verifying:
        raise ValueError('上次操作结果尚未确认，请先重新回查')
    intent = prior if verifying else dict(action=req['action'], value=req.get('value'),
                 request_id=req['request_id'], status='pending', message='正在核验商品', offer=offer)
    if not verifying and prior:
        for key in ('observed_stock', 'observed_price'):
            if key in prior:
                intent[key] = prior[key]
    action, value = intent['action'], intent.get('value')
    bridge = FlowBBridge(ROOT, execute=True)
    targets = json.loads((ROOT / 'flow_b_ef/state/config.json').read_text())['stores']
    async with AsyncExitStack() as stack:
        erp = await stack.enter_async_context(httpx.AsyncClient(base_url='https://api.maozierp.com', transport=FlowBHttpTransport(bridge)))
        base = MaoziZeroStockAdapter(erp)
        registry = SellerStoreRegistry(stack, base, targets, journal, None)
        port = await registry.get(plan['shop_id'])
        product = await port.find_product(plan['shop_id'], offer)
        if product is None or product.sku == '0':
            raise ValueError('平台商品尚未生成，不能执行此操作')
        if product.offer_id != offer:
            raise ValueError('商品身份不匹配')
        async def read_raw():
            data = await port._post('/v3/product/info/list', {'offer_id': [offer]})
            rows = data.get('items', [])
            if len(rows) != 1 or str(rows[0].get('id')) != product.product_id or rows[0].get('offer_id') != offer:
                raise ValueError('商品回查身份不匹配')
            return rows[0]
        raw = await read_raw()
        stocks = await port.read_stocks(product)
        desired = 99 if action == 'activate' else value if action == 'stock' else 0
        if not verifying:
            if action in {'activate', 'price'} or (action == 'stock' and desired > 0):
                if raw.get('is_archived') or raw.get('is_autoarchived') or product.issue_codes:
                    raise ValueError('商品已归档或存在平台错误，需先在平台解决')
                args = tomllib.loads((Path.home()/'.codex/config.toml').read_text())['mcp_servers']['feishu_base']['args']
                fc = await stack.enter_async_context(httpx.AsyncClient(base_url='https://base-api.feishu.cn/open-apis/bitable/v1/', headers={'Authorization':'Bearer '+args[args.index('-p')+1]},timeout=25))
                blocks = await ProductionBlocks(FeishuDelistFeedback(fc,args[args.index('-a')+1]),ROOT/'flow_b_ef/state/quarantine.json').read()
                if (plan['sku'] in blocks.source_skus or product.sku in blocks.ozon_skus
                    or (plan['shop_id'], offer) in blocks.store_offers or ('*',offer) in blocks.store_offers):
                    raise ValueError('明确下架清单禁止此商品恢复销售或改价')
                proof = json.loads(Path(plan['evidence_report']).read_text())
                feedback = await bridge.call('feedback', product=proof['product'], source=proof['source'])
                if feedback.get('blocked') or feedback.get('rejected'):
                    raise ValueError('商品质量或人工反馈校验未通过')
            if action == 'price' and raw.get('currency_code') != 'CNY':
                raise ValueError('平台商品币种不是CNY，拒绝按人民币修改')
            intent.update(product_id=product.product_id, sku=product.sku, shop_id=plan['shop_id'],
                          warehouse_id=plan['warehouse_id'], previous_phase=record['phase'],
                          message='已记录操作，等待平台确认', before_price=raw.get('price'))
            save(path, intent)  # Persist before the first write. Crashes remain pending.
            try:
                if action == 'price':
                    response = await port.client.post('/v1/product/import/prices',json={'prices':[{
                        'product_id':int(product.product_id), 'offer_id':offer,
                        'price':f'{value:.2f}', 'currency_code':'CNY'}]})
                    response.raise_for_status()
                    ack = response.json().get('result', [])
                    if len(ack) != 1 or ack[0].get('updated') is not True or ack[0].get('errors'):
                        raise ValueError('平台未确认价格修改，请回查')
                else:
                    warehouses = {plan['warehouse_id']} if action in {'stock','activate'} else ({s.warehouse_id for s in stocks} | {plan['warehouse_id']})
                    payload = {'stocks':[{'product_id':int(product.product_id), 'offer_id':offer,
                              'warehouse_id':int(w), 'stock':desired} for w in warehouses]}
                    response = await port.client.post('/v2/products/stocks',json=payload)
                    response.raise_for_status()
                    ack = response.json().get('result', [])
                    if len(ack) != len(warehouses) or any(x.get('updated') is not True or x.get('errors') for x in ack):
                        raise ValueError('平台未确认库存修改，请回查')
                intent['message'] = '请求已提交，正在回查'
                save(path, intent)
            except Exception:
                intent['message'] = '提交结果未确认，请重新回查；不会自动重复提交'
                save(path, intent)
                return intent
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(3)
            raw = await read_raw()
            if action == 'price':
                confirmed = str(raw.get('currency_code')) == 'CNY' and abs(float(raw.get('price', -1))-value) < 0.001
                if confirmed:
                    intent.update(status='verified', message='价格已回查确认', observed_price=value)
                    break
            else:
                current = await port.read_stocks(product)
                if action in {'archive','delist'}:
                    confirmed = all(s.present == 0 for s in current)
                else:
                    exact = [s for s in current if s.warehouse_id == plan['warehouse_id']]
                    confirmed = len(exact) == 1 and exact[0].present == desired
                if not confirmed:
                    continue
                if action == 'archive' and not (raw.get('is_archived') or raw.get('is_autoarchived')):
                    if intent.get('archive_requested'):
                        continue
                    if verifying:
                        intent.update(status='not_submitted', message='库存已清零，归档尚未提交，可再次选择归档')
                        break
                    # Zero stock verified first; resolve the exact ERP record before archive.
                    rows = await base._rows('/api.product.online/lists',shop_id=plan['shop_id'],offer_id=offer,archived_type='all')
                    exact = [r for r in rows if str(r.get('shop_id')) == plan['shop_id'] and r.get('offer_id') == offer and str(r.get('sku')) == product.sku]
                    if len(exact) != 1:
                        intent['message'] = '库存已清零，毛子商品记录尚不能精确定位，等待回查'
                        break
                    intent['archive_requested'] = True
                    save(path,intent)
                    await base._request('POST','/api.product.online/archive',payload={'ids':[int(exact[0]['id'])]})
                    continue
                if desired > 0:
                    fresh = await port.find_product(plan['shop_id'],offer)
                    if fresh.status != 'selling' or fresh.issue_codes:
                        intent['message'] = '库存已确认，平台尚未确认可售，请稍后回查'
                        continue
                intent.update(status='verified', message='归档已回查确认' if action == 'archive' else '库存及商品状态已回查确认', observed_stock=desired)
                break
        save(path,intent)
        return intent


if __name__ == '__main__':
    try:
        print(json.dumps({'ok': True, 'result': asyncio.run(operate(json.load(sys.stdin)))},ensure_ascii=False))
    except Exception as e:
        # Never expose raw HTTP responses or credential-bearing errors to browsers.
        message = str(e) if isinstance(e, ValueError) else '接口核验暂未完成，请稍后回查'
        print(json.dumps({'ok':False,'message':message},ensure_ascii=False))
