import asyncio
import csv
import io
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import INTERVALS, APIError, Monitor, State, inventory_class
from .handling import Handling

STATIC = Path(__file__).parent / 'static'


def product_url(sku):
    value = str(sku or '').strip()
    return f'https://www.ozon.ru/product/{value}/' if re.fullmatch(r'[1-9][0-9]{0,19}', value) else ''


def create_app(directory, *, stores=None, api=None, background=True, demo=False):
    state = State(directory)
    if stores is None:
        path = Path(directory) / 'stores.json'
        stores = json.loads(path.read_text()) if path.exists() else []
    identifiers = set()
    for store in stores:
        if not all(isinstance(store.get(k), str) and store[k].strip()
                   for k in ('id', 'name', 'client_id', 'api_key')):
            raise ValueError('店铺配置不完整')
        if store['id'] in identifiers:
            raise ValueError('店铺ID重复')
        identifiers.add(store['id'])
    monitor = Monitor(state, stores, api=api)
    handling = Handling(state, stores)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    tasks = set()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(monitor.run()) if background else None
        cloud_task = asyncio.create_task(handling.run()) if background and handling.config else None
        if cloud_task:
            tasks.add(cloud_task)
        yield
        for t in list(tasks) + ([task] if task else []):
            t.cancel()
        await asyncio.gather(*list(tasks), *([task] if task else []), return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.monitor, app.state.state, app.state.csrf = monitor, state, csrf
    app.state.handling = handling

    @app.middleware('http')
    async def security(request: Request, call_next):
        host = request.url.hostname
        if host not in ('127.0.0.1', 'localhost', 'testserver'):
            return JSONResponse({'detail': '只接受本机访问'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '不接受跨站请求'}, status_code=403)
        if request.url.path.startswith('/api/'):
            if not secrets.compare_digest(request.cookies.get('ops_session', ''), token):
                return JSONResponse({'detail': '请刷新工作台重新连接'}, status_code=401)
            if request.method not in ('GET', 'HEAD'):
                origin = request.headers.get('origin')
                if origin and origin != str(request.base_url).rstrip('/'):
                    return JSONResponse({'detail': '请求来源不匹配'}, status_code=403)
                if not secrets.compare_digest(request.headers.get('x-ops-csrf', ''), csrf):
                    return JSONResponse({'detail': '会话校验失败，请刷新'}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.get('/')
    def index():
        r = FileResponse(STATIC / 'index.html')
        r.set_cookie('ops_session', token, httponly=True, samesite='strict')
        return r

    app.mount('/assets', StaticFiles(directory=STATIC), name='assets')

    def shop(store_id):
        s = monitor.shop(store_id)
        if not s:
            raise HTTPException(404, '没有找到这家店铺')
        return s

    def selected(store_id):
        return [shop(store_id)] if store_id else stores

    def inventory_rows(store_id=None, q='', category='alerts', include_muted=False):
        rows = []
        with state.connect() as c:
            muted = {(r['store'], r['product']): r['until'] for r in c.execute('SELECT * FROM muted WHERE until>?', (time.time(),))}
        for s in selected(store_id):
            threshold = state.threshold(s['id'])
            snap = state.get(s['id'], 'inventory')
            for item in snap['body']:
                cls = inventory_class(item, threshold)
                ignored = muted.get((s['id'], item['id']), 0)
                if ignored and not include_muted and category != 'muted':
                    continue
                if category == 'alerts' and cls not in ('zero', 'low'):
                    continue
                if category == 'muted' and not ignored:
                    continue
                if category not in ('alerts', 'all', 'muted') and cls != category:
                    continue
                if q and q.lower() not in (item['name'] + item['offer_id'] + item['sku'] + s['name']).lower():
                    continue
                rows.append({**item, 'product_url': product_url(item.get('sku')),
                             'store_id': s['id'], 'store_name': s['name'], 'category': cls,
                             'threshold': threshold, 'muted_until': ignored, 'synced': snap['success'],
                             'stale': bool(snap['error']) or time.time() - snap['success'] > 2 * INTERVALS['inventory']})
        order = {'zero': 0, 'low': 1, 'unknown': 2, 'normal': 3, 'archived': 4}
        return sorted(rows, key=lambda r: (order.get(r['category'], 5), r['store_name'], r['offer_id']))

    @app.get('/api/bootstrap')
    def bootstrap():
        return {'csrf': csrf, 'demo': demo, 'stores': [{'id': s['id'], 'name': s['name']} for s in stores],
                'intervals': INTERVALS, 'cloud_url': handling.cloud_link()}

    @app.get('/api/overview')
    def overview():
        result = []
        inventory = inventory_rows(category='all')
        for s in stores:
            sid = s['id']
            snapshots = {k: state.get(sid, k) for k in INTERVALS}
            statuses = {k: {key: v[key] for key in ('success', 'attempt', 'error')} for k, v in snapshots.items()}
            for k, v in statuses.items():
                v['busy'] = (sid, k) in monitor.busy
                v['stale'] = not v['success'] or bool(v['error']) or time.time() - v['success'] > INTERVALS[k] * 2
            stock = [r for r in inventory if r['store_id'] == sid]
            returns, chats = snapshots['returns']['body'], snapshots['chats']['body']
            buyer = [c for c in chats if c['type'] == 'BUYER_SELLER']
            result.append({'id': sid, 'name': s['name'], 'threshold': state.threshold(sid),
                           'inventory_total': len(snapshots['inventory']['body']),
                           'zero': sum(r['category'] == 'zero' for r in stock),
                           'low': sum(r['category'] == 'low' for r in stock),
                           'unknown': sum(r['category'] == 'unknown' for r in stock),
                           'returns': len(returns), 'open_returns': sum(not r['closed'] for r in returns),
                           'buyer_chats': len(buyer), 'buyer_unread': sum(c['unread'] for c in buyer),
                           'channels': statuses})
        return {'stores': result, 'at': time.time()}

    @app.get('/api/returns')
    def returns(store_id: str | None = None, q: str = '', status: str = 'all',
                since: str = '', offset: int = 0, limit: int = 100, handled: str = 'all'):
        if offset < 0 or not 1 <= limit <= 500:
            raise HTTPException(400, '分页参数错误')
        rows = []
        for s in selected(store_id):
            snap = state.get(s['id'], 'returns')
            for r in snap['body']:
                if status == 'open' and r['closed'] or status == 'closed' and not r['closed']:
                    continue
                if since and (not r['created_at'] or r['created_at'][:10] < since):
                    continue
                if q and q.lower() not in (r['name'] + r['order'] + r['offer_id'] + s['name']).lower():
                    continue
                rows.append({**r, 'product_url': product_url(r.get('sku')),
                             'store_id': s['id'], 'store_name': s['name'], 'synced': snap['success'],
                             'stale': bool(snap['error']) or time.time() - snap['success'] > 2 * INTERVALS['returns']})
        rows.sort(key=lambda x: x['created_at'], reverse=True)
        rows = handling.annotate(rows, 'returns', handled)
        return {'total': len(rows), 'items': rows[offset:offset + limit]}

    @app.get('/api/inventory')
    def inventory(store_id: str | None = None, q: str = '', category: str = 'alerts',
                  offset: int = 0, limit: int = 100, handled: str = 'all'):
        if offset < 0 or not 1 <= limit <= 500:
            raise HTTPException(400, '分页参数错误')
        rows = inventory_rows(store_id, q, category)
        rows = handling.annotate(rows, 'inventory', handled)
        return {'total': len(rows), 'items': rows[offset:offset + limit]}

    class Handle(BaseModel):
        key: str = Field(max_length=1000)
        handled: bool
        version: int = Field(ge=0)

    @app.post('/api/handling')
    async def handle(body: Handle):
        try:
            return await handling.set(body.key, body.handled, body.version)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    @app.get('/api/cloud-status')
    def cloud_status():
        return {'configured': bool(handling.config), 'synced': handling.synced, 'error': handling.error}

    @app.get('/api/inventory/export')
    def inventory_export(store_id: str | None = None, q: str = '', category: str = 'alerts'):
        stream = io.StringIO()
        w = csv.writer(stream)
        w.writerow(['店铺', '商品', '货号', 'SKU', '库存', '预留', '分类', '预警阈值', '上次同步', '商品链接'])
        def cell(value):
            text = str(value if value is not None else '')
            return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
        for r in inventory_rows(store_id, q, category):
            w.writerow([cell(r[k]) for k in ('store_name', 'name', 'offer_id', 'sku', 'present', 'reserved', 'category', 'threshold', 'synced', 'product_url')])
        return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="inventory-alerts.csv"'})

    @app.get('/api/chats')
    def chats(store_id: str | None = None, category: str = 'buyer', q: str = ''):
        rows = []
        for s in selected(store_id):
            snap = state.get(s['id'], 'chats')
            for c in snap['body']:
                if category == 'buyer' and c['type'] != 'BUYER_SELLER':
                    continue
                if category == 'unread' and (c['type'] != 'BUYER_SELLER' or not c['unread']):
                    continue
                if category == 'system' and c['type'] == 'BUYER_SELLER':
                    continue
                if q and q.lower() not in (s['name'] + c['id']).lower():
                    continue
                rows.append({**c, 'store_id': s['id'], 'store_name': s['name'], 'synced': snap['success'],
                             'stale': bool(snap['error']) or time.time() - snap['success'] > 120})
        rows.sort(key=lambda c: (bool(c['unread']), c['created_at']), reverse=True)
        return {'items': rows}

    @app.get('/api/chats/{store_id}/{chat_id}/history')
    async def history(store_id: str, chat_id: str, before: str | None = None):
        try:
            d = await monitor.history(store_id, chat_id, before)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except APIError as e:
            raise HTTPException(502, str(e)) from None
        with state.connect() as c:
            d['outbox'] = [dict(r) for r in c.execute(
                'SELECT id,body,status,message_id,error,at FROM outbox WHERE store=? AND chat=? ORDER BY at DESC LIMIT 20',
                (store_id, chat_id))]
        return d

    class Message(BaseModel):
        text: str = Field(min_length=1, max_length=1000)
        request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,80}$')

    @app.post('/api/chats/{store_id}/{chat_id}/send')
    async def send(store_id: str, chat_id: str, payload: Message):
        try:
            return await monitor.send(store_id, chat_id, payload.text, payload.request_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    class Resolution(BaseModel):
        outcome: str = Field(pattern=r'^(confirmed_sent|confirmed_not_sent)$')

    @app.post('/api/outbox/{request_id}/resolve')
    def resolve(request_id: str, payload: Resolution):
        # Explicit local reconciliation only; this endpoint never sends a message.
        with state.connect() as c:
            row = c.execute('SELECT * FROM outbox WHERE id=?', (request_id,)).fetchone()
            if not row or row['status'] != 'uncertain':
                raise HTTPException(409, '只有发送结果待核实的消息可以处理')
            status = 'sent' if payload.outcome == 'confirmed_sent' else 'failed'
            c.execute('UPDATE outbox SET status=?,error=? WHERE id=?',
                      (status, '用户已核对平台消息：' + payload.outcome, request_id))
        return {'ok': True, 'status': status}

    class Setting(BaseModel):
        threshold: int = Field(ge=1, le=10000)

    @app.post('/api/stores/{store_id}/settings')
    def settings(store_id: str, payload: Setting):
        shop(store_id)
        with state.connect() as c:
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (store_id, payload.threshold))
        return {'ok': True}

    class Mute(BaseModel):
        product: str = Field(min_length=1, max_length=100)
        days: int = Field(ge=0, le=365)

    @app.post('/api/stores/{store_id}/mute')
    def mute(store_id: str, payload: Mute):
        shop(store_id)
        if not any(i['id'] == payload.product for i in state.get(store_id, 'inventory')['body']):
            raise HTTPException(404, '商品不属于所选店铺')
        with state.connect() as c:
            c.execute('INSERT OR REPLACE INTO muted VALUES(?,?,?)',
                      (store_id, payload.product, time.time() + payload.days * 86400))
        return {'ok': True}

    last_refresh = {}

    @app.post('/api/refresh')
    async def refresh(store_id: str | None = None):
        targets = selected(store_id)
        key = store_id or 'all'
        if time.time() - last_refresh.get(key, 0) < 30:
            return {'ok': True, 'message': '同步已排队'}
        last_refresh[key] = time.time()
        for s in targets:
            for kind in INTERVALS:
                t = asyncio.create_task(monitor.collect(s, kind))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        return {'ok': True, 'message': '已开始同步，数据会自动更新'}

    @app.get('/api/events')
    def events(after: int = 0):
        with state.connect() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100', (after,))]
            latest = c.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        return {'items': [{**r, 'store_name': shop(r['store'])['name']} for r in rows if monitor.shop(r['store'])],
                'cursor': rows[-1]['id'] if rows else latest}

    return app
