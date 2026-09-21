"""Move unsubmitted website listing intents to another previously routed store."""
import json,re,time
from .maozi import MaoziPublisher


def exhausted(quota):
    values=[re.fullmatch(r'(-?\d+)/(\d+)',str(quota.get(k))) for k in ('total','daily_create')]
    return all(values) and any(int(v[1])<=0 for v in values)


async def choose(db,key,body,api,original,quota):
    from .plugin_publication import require_quota
    from .listing_controls import owned_targets
    if not exhausted(quota):raise ValueError('target_quota_unavailable')
    with db.connect() as c:
        if c.execute('SELECT 1 FROM plugin_publications WHERE owner=? AND sku=?',(key[0],key[1])).fetchone() or c.execute('SELECT 1 FROM jobs WHERE owner=? AND source_key=?',(key[0],key[1])).fetchone():
            raise ValueError('已有发布记录，需回查原店结果，不能自动换店重复发布')
        stores=[dict(x) for x in c.execute('''SELECT s.* FROM stores s WHERE s.owner=? AND s.verified=1 AND s.kind='maozi' AND s.id!=?
            AND EXISTS(SELECT 1 FROM plugin_routes r WHERE r.owner=s.owner AND r.store_id=s.id)
            ORDER BY s.position,s.name''',(key[0],original['id']))]
    shops={str(s['id']):s for s in await api.rows('/api.shop/lists',{'scene':'erp'})}
    checks=[]
    for store in stores:
        cfg=json.loads(store['config']);shop=shops.get(str(cfg.get('shop_id')))
        if not shop or shop.get('status') not in (1,'1') or shop.get('currency')!='CNY':continue
        if str(shop.get('watermark_id'))!=str(cfg.get('watermark_id')):continue
        warehouses=[w for w in shop.get('warehouse',[]) if str(w.get('warehouse_id'))==str(cfg.get('warehouse_id')) and '嘉兴邮政' in w.get('name','')]
        if len(warehouses)!=1:continue
        candidate=MaoziPublisher({'store':{'config':cfg,'credentials':db.open(store['secret'])}})
        try:
            available=await candidate.erp('POST','/api.shop/sync_single_product_limit',body={'id':int(cfg['shop_id'])})
            require_quota(available)
        except Exception as exc:
            checks.append({'store':store['name'],'error':str(exc)[:120]});continue
        # Recheck own import records through this account immediately before changing route.
        if await owned_targets(candidate,cfg,None,key[1]):
            raise ValueError('发现本店导入记录，需回查原结果，暂停换店避免重复发布')
        now=time.time()
        event={'at':now,'from_store_id':original['id'],'from_store_name':original['name'],'to_store_id':store['id'],'to_store_name':store['name'],'reason':'target_quota_unavailable','quota':available}
        with db.write_transaction() as c:
            if c.execute('SELECT 1 FROM plugin_publications WHERE owner=? AND sku=?',(key[0],key[1])).fetchone() or c.execute('SELECT 1 FROM jobs WHERE owner=? AND source_key=?',(key[0],key[1])).fetchone():raise ValueError('发布状态已变化，取消自动换店')
            changed=c.execute('UPDATE plugin_routes SET store_id=?,expires=?,run_id=? WHERE owner=? AND sku=? AND seller=? AND store_id=?',(store['id'],now+86400,body['id'],*key,original['id'])).rowcount
            if changed!=1:raise ValueError('目标店铺已变化，请重新回查')
            body.setdefault('store_switches',[]).append(event);body['quota']=available
            body['actual_store_name']=store['name'];body.pop('error',None);body.pop('next_attempt_at',None)
            c.execute('UPDATE product_listing_controls SET body=? WHERE owner=? AND sku=? AND seller=?',(json.dumps(body),*key))
        return candidate,cfg,None,store
    body.update(phase='capacity_wait',error='可切换店铺暂无已确认的可用额度，稍后自动检查',quota_checks=checks,next_attempt_at=time.time()+300)
    return None
