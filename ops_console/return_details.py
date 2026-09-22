"""Read-only return reasons and evidence-based order stage classification."""
import json
import time

from .core import APIError

STAGES = {'before_ship': '发货前取消', 'refused': '交付时拒收',
          'uncollected': '未领取退回', 'after_delivery': '签收后售后',
          'after_ship': '发货后取消／退回（签收待核实）', 'unknown': '订单阶段待核实'}
TRANSLATIONS = {
    'Покупатель отказался при вручении: товар не подошел': '买家在交付时拒收：商品不合适',
    'Вы не отгрузили заказ вовремя': '卖家未按时发货',
    'Привезли не тот товар': '收到的商品不对／发错货',
    'Вы отменили заказ': '卖家取消订单',
    'Товар не подошёл': '商品不合适',
    'Товар не подошел': '商品不合适',
    'Товар закончился на складе': '仓库商品缺货',
    'Покупатель отменил заказ': '买家取消订单',
    'Товар использовали до меня': '买家反馈商品有使用痕迹',
    'Нет части товара или комплекта': '商品／套装缺少部件',
    'Подделка': '买家反馈疑似假货',
    'Не работает или работает плохо': '商品无法正常工作／性能异常',
    'Проверка товара на соответствие описанию в карточке': '核查商品是否与商品页面描述一致',
    'В заказе есть запрещённые к перевозке товары': '订单包含禁止运输的商品',
}


def reason_cn(reason):
    return TRANSLATIONS.get(reason, '平台原因见原文（暂无核准中文说明）' if reason else '平台暂未提供具体原因')


def classify(posting, reason):
    cancellation = posting.get('cancellation') or {}
    after = cancellation.get('cancelled_after_ship')
    reason_lower = (reason + ' ' + str(cancellation.get('cancel_reason') or '')).lower()
    if 'отказался при вручении' in reason_lower:
        return 'refused', '平台原因明确写明买家在交付时拒收'
    if any(text in reason_lower for text in ('не забрал', 'истек срок хранения', 'истёк срок хранения')):
        return 'uncollected', '平台原因明确写明未领取或保管期届满'
    # fact_delivery_date also occurs on cancelled/uncollected parcels, so it
    # is not accepted on its own as evidence of buyer receipt.
    if posting.get('status') == 'delivered':
        return 'after_delivery', '关联订单当前状态为 delivered（平台标记已交付）'
    if posting.get('status') == 'cancelled' and after is False and not posting.get('delivering_date'):
        return 'before_ship', '订单已取消，平台 cancelled_after_ship=false，且未返回开始运输时间'
    if after is True or posting.get('delivering_date'):
        return 'after_ship', '平台确认发货后取消或已开始运输；尚无签收证据'
    return 'unknown', '当前详情不足以确认发货／签收阶段'


class ReturnDetails:
    def __init__(self, state, api):
        self.state, self.api = state, api
        with state.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS return_details(
                store TEXT, kind TEXT, id TEXT, body TEXT, at REAL, error TEXT,
                PRIMARY KEY(store,kind,id))''')

    async def read(self, store, kind, identifier, path, payload):
        with self.state.connect() as c:
            old = c.execute('SELECT * FROM return_details WHERE store=? AND kind=? AND id=?',
                            (store['id'], kind, identifier)).fetchone()
        if old and time.time() - old['at'] < (600 if old['error'] else 3600):
            return json.loads(old['body']), old['error'], old['at']
        error = ''
        try:
            result = await self.api.call(store, path, payload)
            raw = result.get('returns' if kind == 'detail' else 'result')
            if not isinstance(raw, dict):
                raise APIError('详情返回格式异常')
            fields = ('return_reason', 'comment') if kind == 'detail' else (
                'status', 'substatus', 'cancellation', 'delivering_date', 'fact_delivery_date')
            data = {k: raw.get(k) for k in fields}
        except APIError:
            data, error = {}, '平台详情暂未读取成功，稍后重试'
        now = time.time()
        with self.state.connect() as c:
            c.execute('INSERT OR REPLACE INTO return_details VALUES(?,?,?,?,?,?)',
                      (store['id'], kind, identifier, json.dumps(data, ensure_ascii=False), now, error))
        return data, error, now

    async def enrich(self, store, row):
        detail, posting, errors, checked = {}, {}, [], []
        if row['scheme'] == 'rFBS' and row['id'].isdigit():
            detail, error, at = await self.read(store, 'detail', row['id'], '/v2/returns/rfbs/get',
                                               {'return_id': int(row['id'])})
            if error:
                errors.append(error)
            checked.append(at)
        if row['order']:
            posting, error, at = await self.read(store, 'posting', row['order'], '/v3/posting/fbs/get',
                                                {'posting_number': row['order'], 'with': {
                                                    'analytics_data': False, 'financial_data': False}})
            if error:
                errors.append(error)
            checked.append(at)
        detail_reason = detail.get('return_reason') or {}
        detail_reason = detail_reason.get('name', '') if isinstance(detail_reason, dict) else ''
        cancellation = posting.get('cancellation') or {}
        cancel_reason = cancellation.get('cancel_reason') or ''
        reason = detail_reason or row.get('reason') or cancel_reason
        source = '售后详情' if detail_reason else '售后列表' if row.get('reason') else '订单取消原因' if cancel_reason else '未提供'
        stage, evidence = classify(posting, reason)
        return {**row, 'reason': reason, 'reason_cn': reason_cn(reason), 'reason_source': source,
                'buyer_comment': detail.get('comment') or '', 'order_cancel_reason': cancel_reason,
                'return_stage': stage, 'return_stage_label': STAGES[stage], 'stage_evidence': evidence,
                'detail_checked_at': min(checked) if checked else 0,
                'detail_error': '；'.join(dict.fromkeys(errors))}
