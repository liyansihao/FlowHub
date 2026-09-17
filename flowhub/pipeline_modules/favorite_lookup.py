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
class PublicationAdapter(FavoriteViews,MaoziProductionAdapter):pass
