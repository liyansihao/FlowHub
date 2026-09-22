import asyncio
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

READS = {
    '/v1/returns/list', '/v2/returns/rfbs/list', '/v4/product/info/stocks',
    '/v3/product/info/list', '/v3/chat/list', '/v3/chat/history',
}
SEND = '/v1/chat/send/message'
INTERVALS = {'chats': 60, 'returns': 600, 'inventory': 900}


class APIError(Exception):
    def __init__(self, message, *, uncertain=False, status=0):
        super().__init__(message)
        self.uncertain, self.status = uncertain, status


class Ozon:
    """TLS-verified system curl. Keys travel over stdin, never process arguments.

    No automatic retries, redirects, imports, stock changes or refund actions.
    A failed write stays uncertain unless the API explicitly rejected it.
    """
    def __init__(self):
        self.slots = asyncio.Semaphore(3)
        self.start_lock = asyncio.Lock()
        self.next_start = 0.0

    async def call(self, store, path, payload, *, send=False):
        if path not in READS and not (send and path == SEND):
            raise ValueError('接口不在运营工作台允许范围内')
        if send and path != SEND:
            raise ValueError('无效的消息发送接口')
        lines = [
            'url = ' + json.dumps('https://api-seller.ozon.ru' + path),
            'header = ' + json.dumps('Client-Id: ' + store['client_id']),
            'header = ' + json.dumps('Api-Key: ' + store['api_key']),
            'header = "Content-Type: application/json"',
            'data = ' + json.dumps(json.dumps(payload)),
        ]
        async with self.slots:
            # Pace the initial 27-shop crawl as well as subsequent refreshes.
            # The concurrency cap alone does not prevent bursts of fast responses.
            async with self.start_lock:
                delay = self.next_start - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self.next_start = time.monotonic() + 1.0
            proc = await asyncio.create_subprocess_exec(
                '/usr/bin/curl', '-q', '--config', '-', '--silent', '--show-error',
                '--connect-timeout', '10', '--max-time', '30', '--max-filesize', '30000000',
                '--write-out', '\n%{http_code}',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                raw, _ = await asyncio.wait_for(proc.communicate('\n'.join(lines).encode()), 35)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                proc.kill()
                await proc.wait()
                raise APIError('连接超时；请稍后检查', uncertain=send) from None
        if proc.returncode:
            raise APIError('网络连接失败，保留上次成功数据', uncertain=send)
        try:
            body, status_text = raw.rsplit(b'\n', 1)
            status = int(status_text)
            data = json.loads(body)
        except (ValueError, TypeError):
            raise APIError('平台返回了无法识别的响应', uncertain=send) from None
        if status != 200:
            reasons = {401: '店铺密钥无效', 403: '权限不足或平台访问限制',
                       429: '平台限流，稍后自动重试读取', 400: '平台拒绝了请求参数'}
            # Never expose arbitrary server text: it can contain credential/header echoes.
            raise APIError(reasons.get(status, f'平台暂不可用（HTTP {status}）'),
                           uncertain=send and (status >= 500 or status < 400), status=status)
        if not isinstance(data, dict):
            raise APIError('平台响应结构异常', uncertain=send)
        return data


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'operations.sqlite3'
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS snapshots(
              store TEXT, kind TEXT, body TEXT NOT NULL DEFAULT '[]',
              success REAL NOT NULL DEFAULT 0, attempt REAL NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '', PRIMARY KEY(store,kind));
            CREATE TABLE IF NOT EXISTS settings(store TEXT PRIMARY KEY, threshold INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS muted(store TEXT, product TEXT, until REAL,
              PRIMARY KEY(store,product));
            CREATE TABLE IF NOT EXISTS seen(store TEXT, chat TEXT, message TEXT, unread INTEGER,
              PRIMARY KEY(store,chat));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
              store TEXT, chat TEXT, message TEXT, at REAL, UNIQUE(store,chat,message));
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, store TEXT, chat TEXT,
              body TEXT, status TEXT, message_id TEXT, error TEXT, at REAL);
            ''')
            # A process restart cannot prove whether an in-flight external write succeeded.
            c.execute("UPDATE outbox SET status='uncertain',error='服务重启，发送结果待核实；请勿重复发送' WHERE status='sending'")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def get(self, store, kind):
        with self.connect() as c:
            r = c.execute('SELECT * FROM snapshots WHERE store=? AND kind=?', (store, kind)).fetchone()
        if not r:
            return {'body': [], 'success': 0, 'attempt': 0, 'error': ''}
        return {**dict(r), 'body': json.loads(r['body'])}

    def save(self, store, kind, data):
        now = time.time()
        with self.connect() as c:
            c.execute('''INSERT INTO snapshots VALUES(?,?,?,?,?,?) ON CONFLICT(store,kind)
                DO UPDATE SET body=excluded.body,success=excluded.success,
                attempt=excluded.attempt,error='' ''',
                      (store, kind, json.dumps(data, ensure_ascii=False), now, now, ''))

    def fail(self, store, kind, error):
        with self.connect() as c:
            c.execute('''INSERT INTO snapshots(store,kind,attempt,error) VALUES(?,?,?,?)
                ON CONFLICT(store,kind) DO UPDATE SET attempt=excluded.attempt,error=excluded.error''',
                      (store, kind, time.time(), str(error)))

    def threshold(self, store):
        with self.connect() as c:
            r = c.execute('SELECT threshold FROM settings WHERE store=?', (store,)).fetchone()
        return r[0] if r else 5

    def chat_events(self, store, chats):
        with self.connect() as c:
            for chat in chats:
                if chat['type'] != 'BUYER_SELLER':
                    continue
                previous = c.execute('SELECT * FROM seen WHERE store=? AND chat=?',
                                     (store, chat['id'])).fetchone()
                message, unread = str(chat['last_message_id']), chat['unread']
                # Establish a baseline without pushing every historical conversation.
                # Current unread buyer messages are included; system notifications never are.
                changed = previous and (previous['message'] != message or unread > previous['unread'])
                if unread > 0 and (not previous or changed):
                    c.execute('INSERT OR IGNORE INTO events(store,chat,message,at) VALUES(?,?,?,?)',
                              (store, chat['id'], message, time.time()))
                c.execute('INSERT OR REPLACE INTO seen VALUES(?,?,?,?)',
                          (store, chat['id'], message, unread))


def inventory_class(item, threshold):
    if item.get('archived'):
        return 'archived'
    if item['present'] is None:
        return 'unknown'
    if item['present'] == 0:
        return 'zero'
    if item['present'] < threshold:
        return 'low'
    return 'normal'


def return_row(item, scheme):
    state = item.get('state') or {}
    if not isinstance(state, dict):
        state = {'state': str(state)}
    visual = item.get('visual') or {}
    product = item.get('product') or {}
    code = state.get('state') or visual.get('status', {}).get('sys_name') or str(item.get('status') or '')
    labels = {
        'MoneyReturned': '已退款', 'Utilized': '已处置', 'Utilizing': '处置中',
        'PartialCompensationReturnedByOzon': '平台已补偿',
        'CompensationReturned': '已补偿', 'Rejected': '已拒绝', 'Cancelled': '已取消',
        'New': '新申请', 'Opened': '待处理', 'AwaitingApprove': '待审核',
        'AwaitingReturn': '等待退回', 'AwaitingReceipt': '待收货',
    }
    closed = code in {'MoneyReturned', 'Utilized', 'PartialCompensationReturnedByOzon',
                      'CompensationReturned', 'Rejected', 'Cancelled', 'Canceled', 'Closed'}
    return {
        'id': str(item.get('return_id') or item.get('id') or item.get('return_number')),
        'scheme': scheme, 'created_at': item.get('created_at') or item.get('logistic', {}).get('return_date') or '',
        'order': item.get('posting_number') or item.get('order_number') or '',
        'name': product.get('name') or '未提供商品名称', 'offer_id': str(product.get('offer_id') or ''),
        'sku': str(product.get('sku') or ''), 'price': product.get('price'),
        'currency': product.get('currency_code') or '',
        'state': code, 'state_label': labels.get(code) or state.get('state_name') or visual.get('status', {}).get('display_name') or code or '状态待核对',
        'closed': closed, 'reason': item.get('return_reason_name') or item.get('reason') or '',
    }


class Monitor:
    def __init__(self, state, stores, api=None):
        self.state, self.stores, self.api = state, stores, api or Ozon()
        self.busy = set()
        self.shop_slots = {k: asyncio.Semaphore(2 if k == 'chats' else 1) for k in INTERVALS}
        self.send_locks = {}

    def shop(self, store_id):
        return next((s for s in self.stores if s['id'] == store_id), None)

    async def collect(self, store, kind):
        key = (store['id'], kind)
        if key in self.busy:
            return
        self.busy.add(key)
        try:
            async with self.shop_slots[kind]:
                data = await getattr(self, 'fetch_' + kind)(store)
                if kind == 'chats':
                    self.state.chat_events(store['id'], data)
                self.state.save(store['id'], kind, data)
        except APIError as e:
            self.state.fail(store['id'], kind, str(e))
        except Exception:
            self.state.fail(store['id'], kind, '同步未完成，已保留上次成功数据')
        finally:
            self.busy.discard(key)

    async def fetch_chats(self, store):
        result, cursor, cursors = [], None, set()
        for _ in range(100):
            body = {'filter': {'chat_status': 'All', 'unread_only': False}, 'limit': 100}
            if cursor:
                body['cursor'] = cursor
            d = await self.api.call(store, '/v3/chat/list', body)
            if not isinstance(d.get('chats'), list):
                raise APIError('聊天列表结构异常')
            for c in d['chats']:
                meta = c['chat']
                result.append({'id': meta['chat_id'], 'type': meta.get('chat_type', 'UNSPECIFIED'),
                               'status': meta.get('chat_status', ''), 'unread': c.get('unread_count', 0),
                               'last_message_id': str(c.get('last_message_id', 0)),
                               'created_at': meta.get('created_at', '')})
            if not d.get('has_next'):
                return list({x['id']: x for x in result}.values())
            cursor = d.get('cursor')
            if not cursor or cursor in cursors:
                raise APIError('聊天分页未完成，保留上次数据')
            cursors.add(cursor)
        raise APIError('聊天列表超出单次同步上限，保留上次数据')

    async def fetch_returns(self, store):
        result = []
        for path, scheme in [('/v1/returns/list', 'FBO/FBS'), ('/v2/returns/rfbs/list', 'rFBS')]:
            cursor, seen = None, set()
            for _ in range(100):
                body = {'filter': {}, 'limit': 100}
                if cursor:
                    body['last_id'] = cursor
                d = await self.api.call(store, path, body)
                rows = d.get('returns')
                if not isinstance(rows, list):
                    raise APIError('售后列表结构异常')
                result.extend(return_row(x, scheme) for x in rows)
                if d.get('has_next') is False or ('has_next' not in d and len(rows) < 100):
                    break
                if not rows:
                    raise APIError('售后分页为空但标记有下一页')
                cursor = rows[-1].get('return_id') if scheme == 'rFBS' else rows[-1].get('id')
                if not cursor or str(cursor) in seen:
                    raise APIError('售后分页未完成，保留上次数据')
                seen.add(str(cursor))
            else:
                raise APIError('售后超出单次同步上限，保留上次数据')
        return list({(x['scheme'], x['id']): x for x in result}.values())

    async def fetch_inventory(self, store):
        result, cursor, cursors = [], None, set()
        for _ in range(100):
            body = {'filter': {'visibility': 'ALL'}, 'limit': 1000}
            if cursor:
                body['cursor'] = cursor
            d = await self.api.call(store, '/v4/product/info/stocks', body)
            items = d.get('items')
            if not isinstance(items, list):
                raise APIError('库存列表结构异常')
            for x in items:
                stocks = x.get('stocks') or []
                result.append({'id': str(x['product_id']), 'offer_id': x.get('offer_id', ''),
                               'sku': str(stocks[0].get('sku', '')) if stocks else '',
                               'present': sum(s.get('present', 0) for s in stocks) if stocks else None,
                               'reserved': sum(s.get('reserved', 0) for s in stocks) if stocks else None,
                               'types': sorted({s.get('type', '') for s in stocks}),
                               'name': '', 'archived': None})
            total = d.get('total_items', d.get('total'))
            if (total is not None and len(result) >= int(total)) or (total is None and len(items) < 1000):
                break
            cursor = d.get('cursor')
            if not items or not cursor or cursor in cursors:
                raise APIError('库存分页未完成，保留上次数据')
            cursors.add(cursor)
        else:
            raise APIError('库存超出单次同步上限，保留上次数据')
        result = list({x['id']: x for x in result}.values())
        # Product descriptions and archived flags are read-only; lack of metadata must be visible.
        index = {x['id']: x for x in result}
        for offset in range(0, len(result), 1000):
            d = await self.api.call(store, '/v3/product/info/list',
                                    {'product_id': [int(x['id']) for x in result[offset:offset + 1000]]})
            if not isinstance(d.get('items'), list):
                raise APIError('商品信息未完整返回，保留上次库存数据')
            for p in d['items']:
                row = index.get(str(p.get('id') or p.get('product_id')))
                if row is not None:
                    row.update(name=p.get('name', ''), archived=p.get('is_archived', False),
                               sku=str(p.get('sku') or row['sku']))
        return result

    def known_chat(self, store_id, chat_id):
        return next((x for x in self.state.get(store_id, 'chats')['body'] if x['id'] == chat_id), None)

    async def history(self, store_id, chat_id, before=None):
        store, chat = self.shop(store_id), self.known_chat(store_id, chat_id)
        if not store or not chat:
            raise ValueError('会话不属于所选店铺')
        body = {'chat_id': chat_id, 'limit': 50, 'direction': 'Backward'}
        if before:
            body['from_message_id'] = int(before)
        d = await self.api.call(store, '/v3/chat/history', body)
        messages = d.get('messages')
        if not isinstance(messages, list):
            raise APIError('消息内容暂不可读')
        return {'has_next': d.get('has_next', False), 'messages': [
            {'id': str(m['message_id']), 'at': m.get('created_at'),
             'type': m.get('user', {}).get('type', ''), 'text': '\n'.join(str(t) for t in m.get('data', [])),
             'image': bool(m.get('is_image')), 'context': m.get('context', {})}
            for m in messages], 'chat': chat}

    async def send(self, store_id, chat_id, text, request_id):
        store, chat = self.shop(store_id), self.known_chat(store_id, chat_id)
        if not store or not chat or chat['type'] != 'BUYER_SELLER':
            raise ValueError('只能回复所选店铺的买家会话')
        if chat.get('status') != 'OPENED':
            raise ValueError('该会话已关闭，不能发送')
        text = text.strip()
        if not text or len(text) > 1000:
            raise ValueError('消息须为1至1000个字符')
        async with self.send_locks.setdefault((store_id, chat_id), asyncio.Lock()):
            with self.state.connect() as c:
                previous = c.execute('SELECT * FROM outbox WHERE id=?', (request_id,)).fetchone()
                if previous:
                    if (previous['store'], previous['chat'], previous['body']) != (store_id, chat_id, text):
                        raise ValueError('发送编号已被其他消息使用')
                    return dict(previous)
                unresolved = c.execute("SELECT 1 FROM outbox WHERE store=? AND chat=? AND status IN ('sending','uncertain')",
                                       (store_id, chat_id)).fetchone()
                if unresolved:
                    raise ValueError('此会话有发送结果待核实的消息，请先核对平台记录')
                c.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?)',
                          (request_id, store_id, chat_id, text, 'sending', '', '', time.time()))
            try:
                d = await self.api.call(store, SEND, {'chat_id': chat_id, 'text': text}, send=True)
                message_id = d.get('result', {}).get('message_id') if isinstance(d.get('result'), dict) else d.get('message_id')
                if not message_id:
                    raise APIError('平台已接收请求，但未返回消息编号；请先核实', uncertain=True)
                status, error = 'sent', ''
            except APIError as e:
                status, error, message_id = ('uncertain' if e.uncertain else 'failed'), str(e), ''
            except BaseException:
                with self.state.connect() as c:
                    c.execute("UPDATE outbox SET status='uncertain',error='发送中断，结果待核实' WHERE id=?", (request_id,))
                raise
            with self.state.connect() as c:
                c.execute('UPDATE outbox SET status=?,message_id=?,error=? WHERE id=?',
                          (status, str(message_id), error, request_id))
                return dict(c.execute('SELECT * FROM outbox WHERE id=?', (request_id,)).fetchone())

    async def run(self):
        # Separate loops keep message checks independent of the initial inventory crawl.
        async def lane(kind):
            while True:
                due = []
                for store in self.stores:
                    snap = self.state.get(store['id'], kind)
                    interval = max(INTERVALS[kind], 180) if snap['error'] else INTERVALS[kind]
                    if time.time() - snap['attempt'] >= interval:
                        due.append(self.collect(store, kind))
                if due:
                    await asyncio.gather(*due)
                await asyncio.sleep(5)
        await asyncio.gather(*(lane(k) for k in INTERVALS))
