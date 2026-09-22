"""Internal handling flags and a credential-free cloud snapshot mirror."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

from .core import INTERVALS, inventory_class


def row_key(kind, store, item):
    return json.dumps([kind, store, str(item.get('scheme') or ''), str(item['id'])],
                      ensure_ascii=False, separators=(',', ':'))


class Handling:
    def __init__(self, state, stores):
        self.state, self.stores = state, stores
        self.lock = asyncio.Lock()
        self.error, self.synced = '', 0
        self.last_hash, self.uploaded = '', 0
        path = Path(state.directory) / 'cloud.json'
        self.config = json.loads(path.read_text()) if path.exists() else None
        if self.config and self.config['url'] != 'https://flowhub-review.vercel.app/api/operations':
            raise ValueError('运营云端地址不匹配')
        with state.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS handling(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS handling_audit(id INTEGER PRIMARY KEY, key TEXT, value TEXT)')

    def marks(self):
        with self.state.connect() as c:
            return {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM handling')}

    def annotate(self, rows, kind, status='all'):
        marks = self.marks()
        result = []
        for r in rows:
            key = row_key(kind, r['store_id'], r)
            mark = marks.get(key, {'handled': False, 'version': 0})
            if status == 'pending' and mark['handled'] or status == 'done' and not mark['handled']:
                continue
            result.append({**r, 'handling_key': key, 'handling': mark})
        return result

    def cloud_link(self):
        if not self.config:
            return ''
        return 'https://flowhub-review.vercel.app/operations/'

    async def request(self, mode='view', body=None):
        lines = ['url = ' + json.dumps(self.config['url'] + '?mode=' + mode),
                 'header = ' + json.dumps('X-Sync-Token: ' + self.config['token']),
                 'header = "Content-Type: application/json"']
        if body is not None:
            lines.append('data = ' + json.dumps(json.dumps(body, ensure_ascii=False), ensure_ascii=False))
        proc = await asyncio.create_subprocess_exec(
            '/usr/bin/curl', '-q', '--config', '-', '--silent', '--show-error',
            '--connect-timeout', '10', '--max-time', '40', '--write-out', '\n%{http_code}',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            raw, _ = await asyncio.wait_for(proc.communicate('\n'.join(lines).encode()), 45)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        try:
            body, code = raw.rsplit(b'\n', 1)
            data = json.loads(body)
        except (ValueError, TypeError):
            raise ValueError('云端连接失败，请稍后刷新核对') from None
        if proc.returncode or int(code) != 200:
            raise ValueError('云端记录已变化或连接失败，请刷新后重试')
        return data

    def snapshot(self):
        items, stores = [], []
        for s in self.stores:
            channels = {}
            for kind in ('returns', 'inventory'):
                snap = self.state.get(s['id'], kind)
                stale = bool(snap['error']) or time.time() - snap['success'] > 2 * INTERVALS[kind]
                channels[kind] = {'success': snap['success'], 'stale': stale}
                for r in snap['body']:
                    if kind == 'inventory' and inventory_class(r, 5) != 'zero':
                        continue
                    items.append({**r, 'id': str(r['id']), 'kind': kind, 'store_id': s['id'],
                                  'store_name': s['name'], 'synced': snap['success'], 'stale': stale})
            stores.append({'id': s['id'], 'name': s['name'], 'channels': channels})
        return {'items': items, 'stores': stores}

    def save_marks(self, marks):
        with self.state.connect() as c:
            for mark in marks:
                c.execute('INSERT OR REPLACE INTO handling VALUES(?,?)',
                          (mark['key'], json.dumps(mark, ensure_ascii=False)))

    async def sync(self):
        if not self.config:
            return
        async with self.lock:
            snapshot = self.snapshot()
            digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
            if digest != self.last_hash or time.time() - self.uploaded > 300:
                await self.request('snapshot', snapshot)
                self.last_hash, self.uploaded = digest, time.time()
            data = await self.request('markers')
            self.save_marks(data['markers'])
            self.synced, self.error = time.time(), ''

    async def run(self):
        while True:
            try:
                await self.sync()
            except Exception:
                self.error = '云端同步延迟，保留上次处理状态'
            await asyncio.sleep(60)

    async def set(self, key, handled, version):
        async with self.lock:
            if not any(row_key(r['kind'], r['store_id'], r) == key for r in self.snapshot()['items']):
                raise ValueError('记录不存在或已不再是零库存，请刷新')
            if self.config:
                try:
                    result = await self.request('handle', {'key': key, 'handled': handled, 'version': version})
                except ValueError:
                    data = await self.request('markers')
                    self.save_marks(data['markers'])
                    raise
            else:
                previous = self.marks().get(key, {'version': 0})
                if previous['version'] != version:
                    raise ValueError('处理状态已变化，请刷新')
                result = {'key': key, 'handled': handled, 'version': version + 1,
                          'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            self.save_marks([result])
            with self.state.connect() as c:
                c.execute('INSERT INTO handling_audit(key,value) VALUES(?,?)', (key, json.dumps(result)))
            return result
