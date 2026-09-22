"""Deterministic test data. This adapter never accesses a network."""
from datetime import datetime, timezone

from .core import State


class DemoAPI:
    def __init__(self, state):
        self.state = state
        self.sent = []

    async def call(self, store, path, body, *, send=False):
        sid = store['id']
        if path == '/v3/chat/history':
            return {'has_next': False, 'messages': [
                {'message_id': 1001, 'user': {'type': 'Customer'}, 'created_at': '2026-09-19T01:03:00Z',
                 'data': ['Здравствуйте! Подскажите, пожалуйста, когда будет отправлен мой заказ?'],
                 'context': {'order_number': 'TEST-482601-001'}},
                {'message_id': 1002, 'user': {'type': 'Customer'}, 'created_at': '2026-09-19T01:05:00Z',
                 'data': ['Спасибо! Буду ждать вашего ответа.']},
                *[m for m in self.sent if m['chat_id'] == body['chat_id'] and m['store_id'] == sid],
            ]}
        if path == '/v1/chat/send/message' and send:
            message_id = 2000 + len(self.sent)
            self.sent.append({'message_id': message_id, 'user': {'type': 'Seller'},
                              'data': [body['text']], 'chat_id': body['chat_id'], 'store_id': sid,
                              'created_at': datetime.now(timezone.utc).isoformat()})
            return {'result': {'message_id': message_id}}
        if path == '/v3/chat/list':
            return {'chats': [{'chat': {'chat_id': x['id'], 'chat_type': x['type'],
                                       'chat_status': x['status'], 'created_at': x['created_at']},
                               'unread_count': x['unread'], 'last_message_id': x['last_message_id']}
                              for x in self.state.get(sid, 'chats')['body']], 'has_next': False}
        if path == '/v4/product/info/stocks':
            items = self.state.get(sid, 'inventory')['body']
            return {'items': [{'product_id': x['id'], 'offer_id': x['offer_id'],
                               'stocks': [] if x['present'] is None else [{'present': x['present'],
                                          'reserved': x['reserved'], 'sku': x['sku'], 'type': 'rfbs'}]}
                              for x in items], 'total': len(items)}
        if path == '/v3/product/info/list':
            return {'items': [{'id': x['id'], 'name': x['name'], 'is_archived': x['archived']}
                              for x in self.state.get(sid, 'inventory')['body']]}
        if path in ('/v1/returns/list', '/v2/returns/rfbs/list'):
            if path == '/v1/returns/list':
                return {'returns': [], 'has_next': False}
            return {'returns': [{'return_id': x['id'], 'created_at': x['created_at'],
                                 'posting_number': x['order'], 'product': {'name': x['name'],
                                 'price': x['price'], 'currency_code': x['currency'], 'offer_id': x['offer_id']},
                                 'state': {'state': x['state']}} for x in self.state.get(sid, 'returns')['body']]}
        raise AssertionError('Unexpected demo endpoint')


def setup(directory):
    state = State(directory)
    names = ['丽丽1号', '丽丽二号', '好好1号', '精铺一店', '姜总1号', '公司1号', '臻贞1号', '双姐1号']
    products = ['透明防摔手机壳 / iPhone 15 Pro', 'USB Type-C 编织充电线 2m', '汽车内饰皮革护理剂 100ml',
                '不锈钢旅行保温杯 500ml', '宠物轻薄透气背心 / 绿色', '桌面折叠手机支架']
    stores = []
    for i, name in enumerate(names):
        sid = f'demo-{i+1}'
        stores.append({'id': sid, 'name': name, 'client_id': f'test-{i}', 'api_key': 'demo-no-network'})
        inventory = [{'id': str(10000+i*100+j), 'offer_id': f'TEST-{i+1}-{j+1:04}',
                      'sku': str(5100000000+i*100+j), 'name': products[j%6],
                      'present': None if j%13==0 else 0 if j%7==0 else 3 if j%9==0 else 99,
                      'reserved': 0, 'types': ['rfbs'], 'archived': j==14} for j in range(30+i*3)]
        state.save(sid, 'inventory', inventory)
        state.save(sid, 'returns', [{'id': f'{i+1}{j+1:03}', 'scheme': 'rFBS',
                     'created_at': f'2026-09-{19-j:02}T00:30:00Z', 'order': f'TEST-4826{i}{j}-001',
                     'name': products[(i+j)%6], 'offer_id': f'TEST-{i+1}-{j+1:04}', 'sku': '',
                     'price': [27.05, 13.06, 32, 49.90][j%4], 'currency': 'CNY',
                     'state': 'MoneyReturned' if j%2 else 'AwaitingApprove',
                     'state_label': '已退款' if j%2 else '待审核', 'closed': bool(j%2),
                     'reason': '商品与预期不符（演示）'} for j in range(i%4+1)])
        state.save(sid, 'chats', [{'id': f'demo-chat-{i+1}', 'type': 'BUYER_SELLER',
                                 'status': 'OPENED', 'unread': 2 if i%3==0 else 0,
                                 'last_message_id': '1002', 'created_at': '2026-09-19T01:03:00Z'}])
    return stores, DemoAPI(state)
