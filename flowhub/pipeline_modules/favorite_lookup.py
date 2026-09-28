"""Explicit imported/unimported favorite views; truncated pages never prove absence."""
from flowef.adapters.erp.maozi_test_listing import MaoziZeroStockAdapter
from flowef.adapters.erp.production_listing import MaoziProductionAdapter
from flowef.application.errors import ExternalContractError


class FavoriteViews:
    async def _rows(self, endpoint, **params):
        if endpoint!='/api.product.favorite/lists':return await super()._rows(endpoint,**params)
        found={}
        for imported in (0,1):
            seen=set();expected=None
            for page in range(1,101):
                data=await self._request('GET',endpoint,params={**params,'is_imported':imported,'page':page,'page_size':100})
                rows=data if isinstance(data,list) else next((data[k] for k in ('data','list','rows','items') if isinstance(data.get(k),list)),None) if isinstance(data,dict) else None
                if rows is None:raise ExternalContractError('invalid favorite listing')
                total=data.get('total') if isinstance(data,dict) else None
                if total is not None:
                    if not str(total).isdigit():raise ExternalContractError('invalid favorite total')
                    total=int(total)
                    if expected is not None and total!=expected:raise ExternalContractError('favorite listing changed; absence unconfirmed')
                    expected=total
                for row in rows:
                    if not isinstance(row,dict) or not str(row.get('id','')).isdigit():raise ExternalContractError('invalid favorite identity')
                    key=str(row['id'])
                    if key in seen:raise ExternalContractError('favorite pagination repeated; absence unconfirmed')
                    seen.add(key);found[key]=row
                if total is not None:
                    if len(seen)>total or not rows and len(seen)<total:raise ExternalContractError('incomplete favorite listing')
                    if len(seen)==total:break
                elif not rows:break
            else:raise ExternalContractError('favorite pagination limit; absence unconfirmed')
        return list(found.values())


class PublicationSourceAdapter(FavoriteViews,MaoziZeroStockAdapter):pass
class PublicationAdapter(FavoriteViews,MaoziProductionAdapter):
    async def add_favorite(self, plan):
        return await _capacity_add(self, plan)


# A documented business rejection of favorite creation is not an unknown write.
from flowef.application.errors import RequestNotSent
import asyncio
import json
import time
import weakref

_capacity_locks = weakref.WeakKeyDictionary()


class FavoriteCapacityWait(RequestNotSent):
    pass


def capacity_rejection(message):
    import re
    message = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m[1], 16)), str(message))
    return '/api.product.favorite/toggle' in message and '收藏数量已达上限' in message


async def _capacity_add(self, plan):
    from .database_work import run as database_work
    from .favorite_reclaim import schema, observe
    context = getattr(self, 'favorite_capacity_context', None)

    async def send():
        try:
            return await super(PublicationAdapter, self).add_favorite(plan)
        except ExternalContractError as error:
            if not capacity_rejection(error):
                raise
            if context:
                db, account = context
                def block():
                    with db.connect() as c:
                        schema(c)
                        c.execute('UPDATE favorite_capacity_state SET blocked_until=? WHERE account=?', (time.time()+60, account))
                await database_work(block)
            raise FavoriteCapacityWait('waiting_favorite_capacity: explicit ERP rejection') from error

    if not context:
        return await send()
    db, account = context
    path = db.directory / 'favorite-cleanup.json'
    policy = json.loads(path.read_text()) if path.exists() else {}
    if policy.get('mode') != 'reclaim_unused':
        return await send()
    locks = _capacity_locks.setdefault(asyncio.get_running_loop(), {})
    async with locks.setdefault(account, asyncio.Lock()):
        def read():
            with db.connect() as c:
                schema(c)
                row = c.execute('SELECT * FROM favorite_capacity_state WHERE account=?', (account,)).fetchone()
                return dict(row) if row else None
        row = await database_work(read)
        if row and row['blocked_until'] > time.time():
            raise FavoriteCapacityWait('waiting_favorite_capacity')
        if not row or not 0 <= time.time()-row['observed'] < 30:
            header = await self._request('GET', '/api.product.favorite/lists', params={'page': 1, 'page_size': 1})
            await database_work(observe, db, account, header)
        def reserve():
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT * FROM favorite_capacity_state WHERE account=?', (account,)).fetchone()
                if row['used'] >= row['capacity'] or row['blocked_until'] > time.time():
                    raise FavoriteCapacityWait('waiting_favorite_capacity')
                c.execute('UPDATE favorite_capacity_state SET used=used+1 WHERE account=?', (account,))
        await database_work(reserve)
        return await send()
