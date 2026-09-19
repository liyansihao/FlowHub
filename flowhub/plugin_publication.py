"""Bounded compareBot-to-Maozi production continuation using the existing journal."""
import fcntl
import json
import re
import math
import sys
import time
from dataclasses import asdict
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from .db import DATA
from .maozi import MaoziPublisher
from .source_delists import read_delists
from .source_detail import SourceCollector
from .source_library import SourceLibrary, fingerprint
from .pipeline_modules.pricing import approved_price_intent

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'FlowEF-production/src'))
from flowef.adapters.erp.flowb_bridge import FlowBBridge, FlowBHttpTransport
from flowef.adapters.erp.maozi_test_listing import MaoziZeroStockAdapter
from flowef.adapters.erp.production_listing import MaoziProductionAdapter
from flowef.adapters.persistence.test_listing_journal import TestListingJournal
from flowef.adapters.persistence.production_ownership import ProductionOwnership
from flowef.application.ports.test_listing import ZeroStockListingPlan
from flowef.application.services.test_listing import ProductionListingService
from flowef.domain.rules.platform_issues import classify_product_issues


def require_quota(q):
    for key in ('total','daily_create'):
        m=re.fullmatch(r'(-?\d+)/(\d+)',str(q.get(key)))
        if not m or int(m[1])<=0:raise ValueError('target_quota_unavailable')

def same_postal_package(detail,facts):
    # ChinaPost prices use max/min/sum/volume, independent of edge labels.
    keys=('package_length','package_width','package_height')
    return sorted(float(detail[k])/10 for k in keys)==sorted(float(facts[k]) for k in keys) and float(detail['package_weight'])==float(facts['package_weight'])


def unknown_restrictions_allowed(review):
    monthly=review.get('candidate',{}).get('origin',{}).get('plugin_detail',{}).get('monthly_sales',{})
    return monthly.get('sales_schema') in (None,'','FBS') and monthly.get('blocked_by_seller') in (None,False)


def require_source_modes(modes, monthly, allow_unknown=False, now=None):
    now=time.time() if now is None else now
    if monthly.get('blocked_by_seller') is True:
        raise ValueError('seller_explicitly_blocks_follow')
    if allow_unknown and monthly.get('sales_schema') in (None,'','FBS') and (not modes or tuple(modes)==('FBS',)):
        return
    if tuple(modes)!=('FBS',) and not (not modes and monthly.get('sales_schema')=='FBS' and monthly.get('blocked_by_seller') is False and 0<=now-monthly.get('observed_at',0)<900):
        raise ValueError('fresh_fbs_required')


def approved(review, rules, now=None, allow_unknown=False, *, native_follow=False):
    now = time.time() if now is None else now
    if review.get('state') != 'matched' or not 0 <= now-review.get('finished_at',0) < 21600:
        raise ValueError('fresh_comparebot_approval_required')
    origin=review.get('candidate',{}).get('origin',{})
    quote=origin.get('price_evidence')
    if quote is not None:
        stamp=quote.get('observed_at')
        if not isinstance(stamp,(int,float)) or not 0<=now-stamp<21600:
            if approved_price_intent(review,now) is None:raise ValueError('price_evidence_stale')
    website=bool(review.get('website_listing_authorization',{}).get('id') and review.get('identity_review',{}).get('verdict')=='match' and (review.get('identity_review',{}).get('human_review') or review.get('identity_review',{}).get('automatic_review')))
    blockers=list(review.get('publication_blockers') or [])
    if website or native_follow:blockers=[b for b in blockers if b!='publication_attributes_missing']
    if allow_unknown:
        if not unknown_restrictions_allowed(review):raise ValueError('explicit_source_restriction')
        blockers=[b for b in blockers if b not in ('fresh_pure_fbs_required','follow_permission_unverified_or_blocked')]
    if blockers:
        raise ValueError('publication_checks_required:'+','.join(blockers))
    match=review['result']; evidence=match['evidence']; source=evidence['source']; profit=evidence['profit']
    if source['comparebot']['decision']['outcome']!='approved':
        raise ValueError('comparebot_not_approved')
    rate=float(profit['assessment']['erp_profit_rate_pct'])
    if not math.isfinite(rate) or (not website and rate <= float(rules.get('profit_min',30))):
        raise ValueError('profit_below_workflow_threshold')
    if str(source['selected_offer_id'])!=str(match['supplier_id']) or float(source['selected_cost_cny'])!=float(match['purchase']):
        raise ValueError('supplier_cost_binding_mismatch')
    return evidence


async def advance(db, owner, sku, seller):
    # Exclusive maintenance locks can drain all operations. Independent stores
    # proceed concurrently; one SKU or one target store can never overlap.
    import hashlib
    locks=[]
    try:
        maintenance=(DATA/'plugin-publication.lock').open('a');locks.append(maintenance)
        fcntl.flock(maintenance,fcntl.LOCK_SH|fcntl.LOCK_NB)
        with db.connect() as c:
            prior=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone() else None
            route=c.execute('SELECT store_id FROM plugin_routes WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_routes'").fetchone() else None
            wf=c.execute('SELECT active_store FROM workflows WHERE owner=?',(owner,)).fetchone()
            target=json.loads(prior[0])['store_id'] if prior else route[0] if route else wf[0] if wf else ''
        directory=DATA/'publication-locks';directory.mkdir(exist_ok=True)
        for key in ('sku:'+sku,'store:'+str(target)):
            handle=(directory/(hashlib.sha256(key.encode()).hexdigest()+'.lock')).open('a');locks.append(handle)
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from .follow_publication import selected
        if selected(db,(owner,sku,seller)):
            return await _advance(db,owner,sku,seller,native_follow=True)
        from .official_publication import advance_if_selected
        official = await advance_if_selected(db,owner,sku,seller)
        if official is not None:return official
        return await _advance(db,owner,sku,seller)
    finally:
        for handle in reversed(locks):handle.close()


async def _advance(db, owner, sku, seller, *, native_follow=False):
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))')
        prior=c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        wf=c.execute('SELECT * FROM workflows WHERE owner=?',(owner,)).fetchone()
        row=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        if wf is None or row is None:raise ValueError('review_missing')
        record=json.loads(prior[0]) if prior else None
        if native_follow and record and record.get('backend')!='maozi_follow':
            raise ValueError('historical_publication_backend_mismatch')
        if native_follow and not record and c.execute('SELECT 1 FROM plugin_publications WHERE sku=?',(sku,)).fetchone():
            raise ValueError('source_already_claimed')
        c.execute('CREATE TABLE IF NOT EXISTS plugin_routes(owner TEXT,sku TEXT,seller TEXT,store_id TEXT,expires REAL,run_id TEXT,PRIMARY KEY(owner,sku,seller))')
        route=c.execute('SELECT * FROM plugin_routes WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        review=record['review'] if record else json.loads(row[0])
        rules=json.loads(wf['rules'])
        c.execute('CREATE TABLE IF NOT EXISTS plugin_publication_permissions(owner TEXT,sku TEXT,seller TEXT,expires REAL,reason TEXT,PRIMARY KEY(owner,sku,seller))')
        permission=c.execute('SELECT * FROM plugin_publication_permissions WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        allow_unknown=bool(permission and time.time()<permission['expires'] and route and time.time()<route['expires'])
        if record and fingerprint(json.loads(row[0]))!=fingerprint(review):
            phase=TestListingJournal(DATA/'plugin-production.sqlite3').read(record['offer_id'])['phase']
            if phase in ('prepared','ready','favorite_pending','reconciling','sync_pending','stock_ready','stock_pending') or (phase=='manual_review' and TestListingJournal(DATA/'plugin-production.sqlite3').read(record['offer_id'])['details'].get('reason') in ('reconciliation_timeout','platform_issue_requires_review')):
                latest=json.loads(row[0]);approved(latest,rules,allow_unknown=allow_unknown,native_follow=native_follow)
                from .pipeline_modules.dossier import refresh_unchanged_plan
                try:
                    if native_follow:
                        from .follow_publication import refresh_review
                        refresh_review(record,latest)
                    else:refresh_unchanged_plan(record,latest)
                except ValueError as error:
                    if native_follow or str(error)!='prepared_plan_repricing_required':raise
                    from .pipeline_modules.dossier import refresh_procurement_plan
                    record=refresh_procurement_plan(c,DATA/'plugin-production.sqlite3',record,latest)
                review=latest
                c.execute('UPDATE plugin_publications SET body=?,updated=? WHERE owner=? AND sku=? AND seller=?',
                          (json.dumps(record),time.time(),owner,sku,seller))
        if not record:
            from .acquisition import enabled as acquisition_enabled
            from .dossier_gate import valid as dossier_gate_valid
            if not native_follow and acquisition_enabled(db,sku) and not dossier_gate_valid(c,(owner,sku,seller)):
                return {'phase':'awaiting_dossier','needs_dossier':True,'verified':False,
                        'reason':'publication_dossier_gate_required','missing_fields':[]}
            approved(review,rules,allow_unknown=allow_unknown,native_follow=native_follow)
        target_id=record['store_id'] if record else route['store_id'] if route else wf['active_store']
        if not record and route and time.time()>=route['expires']:raise ValueError('hour_window_closed')
        store=c.execute('SELECT * FROM stores WHERE owner=? AND id=? AND verified=1',(owner,target_id)).fetchone()
        if store is None:raise ValueError('active_verified_store_required')
        if not store['enabled'] and not ((record and record.get('authorized_store_id')==target_id) or (route and route['store_id']==target_id and time.time()<route['expires'])):
            raise ValueError('active_verified_store_required')
        if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone():raise ValueError('product_blocked')
        if not record and c.execute('SELECT 1 FROM jobs WHERE owner=? AND source_key=?',(owner,sku)).fetchone():raise ValueError('existing_source_job')
    config=json.loads(store['config']);keys=db.open(store['secret']);match=review['result'];evidence=match['evidence'];source=evidence['source'];profit=evidence['profit']
    if int(rules.get('stock',99))!=99 or rules.get('logistics')!='ChinaPost':raise ValueError('unsupported_production_route')
    shop=str(config['shop_id']);warehouse=str(config['warehouse_id']);watermark=str(config['watermark_id'])
    journal=TestListingJournal(DATA/'plugin-production.sqlite3')
    ownership=ProductionOwnership(journal,ROOT/'maozi_direct_new_method/state/global-sku-claims','flowhub-plugin-production-v1')
    from .pipeline_modules.request_bridge import PublicationBridge, MeasuredTransport
    bridge=PublicationBridge(ROOT,execute=True,token=keys['erp_token'])
    from .cluster_routing import route_bridge
    route_bridge(bridge, DATA, (owner, sku, seller))
    def save():
        with db.connect() as c:c.execute('INSERT OR REPLACE INTO plugin_publications VALUES(?,?,?,?,?)',(owner,sku,seller,json.dumps(record),time.time()))
    from .pipeline_modules.transport import StepTransport
    transport=StepTransport(MeasuredTransport(bridge),ttl=15,namespace=fingerprint({'token':keys['erp_token']}),
                            circuit_namespace=getattr(bridge,'execution_route','local'))
    async with httpx.AsyncClient(base_url='https://api.maozierp.com',transport=transport) as client, AsyncExitStack() as observation_clients:
        from .pipeline_modules.favorite_lookup import PublicationSourceAdapter, PublicationAdapter
        base=PublicationSourceAdapter(client)
        async def exclusions():
            blocks=await read_delists();offers={tuple(v) for v in blocks['offers']}
            if sku in blocks['skus']:raise ValueError('explicit_delist')
            return blocks,offers
        async def verify_target(plan):
            if record and record.get('write_deadline') and time.time()>=record['write_deadline']:raise ValueError('hour_window_closed')
            live=await base.target(plan.shop_id)
            if not live.shop.active or live.currency!='CNY' or live.watermark_id!=plan.watermark_id:
                raise ValueError('target_identity_changed')
            if not any(w.warehouse_id==plan.warehouse_id and w.active and '嘉兴邮政' in w.name for w in live.shop.warehouses):
                raise ValueError('postal_warehouse_unavailable')
        async def quota(plan,offer):
            if record and record.get('write_deadline') and time.time()>=record['write_deadline']:raise ValueError('hour_window_closed')
            if keys.get('client_id') and keys.get('api_key') and config.get('official_observations',True):
                from .official_api import client as seller_client, capacity
                async with seller_client(db,keys) as seller_api:q=await capacity(seller_api)
            else:
                q=await MaoziPublisher({'store':{'config':config,'credentials':keys}}).erp('POST','/api.shop/sync_single_product_limit',body={'id':int(plan.shop_id)})
            if record is not None:
                record['quota']={'at':time.time(),'data':q};save()
            from .pipeline_modules.store_capacity import observe
            observe(db,owner,target_id,q)
            require_quota(q)
        if not record:
            if route and time.time()>=route['expires']:raise ValueError('hour_window_closed')
            await exclusions()
            if await base.source_has_imports(sku):raise ValueError('source_already_imported')
            claim=ROOT/'maozi_direct_new_method/state/global-sku-claims'/f'{sku}.json'
            if claim.exists():raise ValueError('source_already_claimed')
            c=review['candidate']|{'price':profit['sell_price_cny']}
            context={'owner':owner,'candidate':c,'match':match,'rules':rules,'store':{'id':store['id'],'config':config,'credentials':keys},'idempotency_key':'plugin-'+sku}
            from .pipeline_modules.dossier import reviewed_snapshot
            if native_follow:
                from .follow_publication import local_inputs, legacy_source_hold
                hold=legacy_source_hold(db,owner,sku,keys['erp_token'])
                if hold:raise ValueError(hold)
                snapshot=local_inputs(review)
            else:snapshot=reviewed_snapshot(review)
            if snapshot is None and review.get('website_listing_authorization'):
                original=review['candidate'];physical=original['origin'].get('plugin_detail') or {}
                dims=physical.get('dimensions_mm') or []
                if len(dims)==3 and all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in [*dims,physical.get('weight_g')]):
                    snapshot={'source_key':sku,'source':'user-confirmed-maozi-native-import','observed_at':physical.get('observed_at'),'detail':{'title':original['title'],'package_length':dims[0],'package_width':dims[1],'package_height':dims[2],'package_weight':physical['weight_g'],'attributes':physical.get('attributes') or []}}
            if snapshot is None:snapshot=await SourceCollector(db,context).collect()
            detail=snapshot['detail'];facts=profit['input']
            if str(snapshot['source_key'])!=sku:raise ValueError('draft_source_mismatch')
            if not same_postal_package(detail,facts):
                raise ValueError('draft_profit_package_mismatch')
            plan=ZeroStockListingPlan(shop_id=shop,sku=sku,title=detail['title'].strip(),cover_image=c['image'],sell_price_cny=str(profit['sell_price_cny']),watermark_id=watermark,warehouse_id=warehouse,strategy_version='flowhub-plugin-production-v1',evidence_report='plugin_reviews:'+fingerprint(review),category_id=review['candidate']['origin']['category_id'],purchase_price_cny=str(source['selected_cost_cny']),stock_target=99,purpose='production',supplier_identity=str(source['selected_offer_id']))
            await verify_target(plan)
            await quota(plan,None)
            offer=journal.prepare(plan)
            if not ownership.acquire(plan.shop_id,offer,sku):raise ValueError('source_claim_conflict')
            record={'owner':owner,'sku':sku,'seller':seller,'store_id':store['id'],'store_name':store['name'],'review':review,'snapshot':snapshot,'plan':plan.to_dict(),'offer_id':offer,'started_at':time.time(),'events':[],'allow_unknown_shipping_follow':allow_unknown,'pricing_intent':approved_price_intent(review),'permission_reason':permission['reason'] if allow_unknown else None}
            if native_follow:record['backend']='maozi_follow'
            save()
            if route:
                record.update(authorized_store_id=target_id,write_deadline=route['expires'],run_id=route['run_id']);save()
        plan=ZeroStockListingPlan(**record['plan']);offer=record['offer_id']
        async def stock_guard(plan,product):
            blocks,offers=await exclusions()
            if not ownership.owns(plan.shop_id,offer,sku):raise ValueError('ownership_changed')
            if product.sku in blocks['skus'] or (plan.shop_id,offer) in offers or ('*',offer) in offers:raise ValueError('explicit_delist')
            from .pipeline_modules.dossier import require_unchanged_source
            with db.connect() as c:
                current=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            if current is None:raise ValueError('current_source_packet_missing')
            require_unchanged_source(review,json.loads(current[0]))
            feedback_product=review['candidate']['origin']|{'allow_unknown_publication':allow_unknown,'approved_listing_price':approved_price_intent(review),'website_listing_authorization':review.get('candidate',{}).get('origin',{}).get('website_listing_authorization')}
            if native_follow:
                from .follow_publication import feedback as follow_feedback
                feedback=await follow_feedback(ROOT,feedback_product,source)
            else:feedback=await bridge.call('feedback',product=feedback_product,source=source)
            if feedback.get('blocked'):raise ValueError('human_feedback_blocked')
            if feedback.get('rejected'):raise ValueError('publication_source_policy_required')
        async def preflight(plan,offer):
            approved(review,rules,allow_unknown=allow_unknown,native_follow=native_follow)
            await verify_target(plan)
            await stock_guard(plan,type('Identity',(),{'sku':''})())
            if journal.read(offer)['phase']!='ready':return
            if await base.source_has_imports(sku):raise ValueError('source_already_imported')
            monthly=review['candidate']['origin'].get('plugin_detail',{}).get('monthly_sales') or {}
            # Explicitly authorized unknown modes do not require an extra lookup.
            # Affirmative restrictions in the existing evidence remain blocking.
            modes=() if allow_unknown and monthly.get('sales_schema') in (None,'') else await base.source_modes(sku)
            require_source_modes(modes,monthly,allow_unknown)
        observations=None;inventory=None
        if config.get('official_observations',True) and keys.get('client_id') and keys.get('api_key'):
            from .official_status import OzonSellerStatusAdapter
            from .official_api import client as seller_client
            official_client=await observation_clients.enter_async_context(seller_client(db,keys))
            observations=OzonSellerStatusAdapter(official_client,shop_id=shop,warehouse_id=warehouse)
            await observations.verify_identity()
            if config.get('official_inventory',True):
                from .official_inventory import ScheduledInventoryAdapter
                inventory=ScheduledInventoryAdapter(official_client,shop_id=shop,warehouse_id=warehouse,journal=journal,stock_guard=stock_guard)
                inventory.identity_verified=True
        port=PublicationAdapter(client,journal,stock_guard,verify_target,publish_guard=quota,observations=observations,inventory=inventory)
        service=ProductionListingService(port,journal,preflight,stock_guard=stock_guard)
        before=journal.read(offer)['phase']
        from .pipeline_modules.favorite_recovery import recover_favorite
        try:
            favorite_handled=await recover_favorite(port,journal,offer,plan,preflight,record['events'])
        except Exception as error:
            cause=error.__cause__ or error
            record['phase']=journal.read(offer)['phase']
            record.setdefault('api_timings',[]).append({'at':time.time(),'calls':transport.timings[:]})
            record['events'].append({'at':time.time(),'from':before,'error':type(error).__name__,'reason':str(cause)[:250]});save()
            raise
        # Official Maozi UI uses repair_images({ids:[ERP record id]}).
        # Persist intent before dispatch; an unknown outcome is never replayed.
        current=journal.read(offer)
        issues=set(current['details'].get('platform_issue_codes') or [])
        image_only=bool('warning_all_image_failed' in issues and issues <= {'DESCRIPTION_DECLINE','warning_all_image_failed'})
        if before=='manual_review' and image_only and not current['details'].get('image_repair_dispatched_at'):
            await exclusions()
            await verify_target(plan)
            product=await port.find_product(plan.shop_id,offer)
            if product and product.offer_id==offer and product.shop_id==plan.shop_id:
                if observations:
                    from .official_inventory import exact_erp_product
                    product=await exact_erp_product(base,product)
                if journal.move(offer,'manual_review','manual_review',image_repair_dispatched_at=time.time(),image_repair_record_id=product.record_id):
                    try:
                        await MaoziPublisher({'store':{'config':config,'credentials':keys}}).erp('POST','/api.product.online/repair_images',body={'ids':[int(product.record_id)]})
                        journal.move(offer,'manual_review','manual_review',image_repair_acknowledged_at=time.time())
                    except Exception as error:
                        journal.move(offer,'manual_review','manual_review',image_repair_error=type(error).__name__)
                    record['phase']='manual_review';save()
                    return {'sku':sku,'offer_id':offer,'phase':'manual_review','verified':False,'retryable_readback':True,'reason':'image_repair_waiting_for_platform'}
        from .pipeline_modules.platform_recovery import recover_platform_warning
        if not favorite_handled and await recover_platform_warning(port,journal,offer,plan,preflight):
            before=journal.read(offer)['phase']
        # Late platform visibility is a read-only reconciliation concern, not a
        # reason to replay a possibly accepted publication request.
        if not favorite_handled and before=='manual_review' and (journal.read(offer)['details'].get('reason')=='reconciliation_timeout' or journal.read(offer)['details'].get('image_repair_dispatched_at')):
            product=await port.find_product(plan.shop_id,offer)
            stocks=await port.read_stocks(product) if product else []
            if product and product.status=='selling' and any(s.warehouse_id==warehouse and s.present==99 for s in stocks):
                journal.move(offer,'manual_review','stock_verified',reason=None,recovered_at=time.time())
            elif product and not product.issue_codes and product.status in ('ready_to_sell','selling','out_of_stock') and time.time()<record.get('write_deadline',0):
                # A current approval is required before retrying inventory writes.
                approved(review,rules,allow_unknown=allow_unknown,native_follow=native_follow)
                journal.move(offer,'manual_review','reconciling',reason=None,waiting_since=time.time(),recovered_at=time.time())
            else:
                record['phase']='manual_review';record['last_reconciliation_at']=time.time();save()
                return {'sku':sku,'offer_id':offer,'phase':'manual_review','verified':False,'retryable_readback':True,'reason':'awaiting_exact_remote_outcome'}
            before=journal.read(offer)['phase']
        if (not favorite_handled and before not in ('stock_verified','failed','manual_review')) or (favorite_handled and journal.read(offer)['phase']=='ready'):
            try:
                await service.advance(offer)
                if before=='prepared' and journal.read(offer)['phase']=='favorite_pending':
                    # Verify a successful favorite creation in this same operation.
                    # Some favorites disappear before a later queue turn can see them.
                    await recover_favorite(port,journal,offer,plan,preflight,record['events'])
                    if journal.read(offer)['phase']=='ready':
                        # Consume the confirmed favorite without another queue wait;
                        # advance still runs the original preflight and import CAS.
                        await service.advance(offer)
            except Exception as error:
                cause=error.__cause__ or error
                record['phase']=journal.read(offer)['phase']
                record.setdefault('api_timings',[]).append({'at':time.time(),'calls':transport.timings[:]})
                record['events'].append({'at':time.time(),'from':before,'error':type(error).__name__,'reason':str(cause)[:250]});save()
                if isinstance(cause,ValueError) and str(cause) in ('target_quota_unavailable','hour_window_closed'):raise cause
                raise
        final=journal.read(offer)
        record.setdefault('api_timings',[]).append({'at':time.time(),'calls':transport.timings[:]})
        record['events'].append({'at':time.time(),'from':before,'to':final['phase']});record['phase']=final['phase'];save()
        if final['phase']=='stock_verified':
            product=await port.find_product(plan.shop_id,offer);stocks=await port.read_stocks(product) if product else []
            record['product']=asdict(product) if product else None;record['stocks']=[asdict(s) for s in stocks]
            record['verified']=bool(product and product.status=='selling' and any(s.warehouse_id==warehouse and s.present==99 for s in stocks));save()
        library=SourceLibrary(db)
        with db.connect() as c:
            row=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
            p=json.loads(row[0]);p.setdefault('listing_review',{}).update(observed_at=time.time(),label='已回查可售，库存99' if record.get('verified') else '毛子正式上架：'+final['phase'],publication_ready=True,publication={'shop_id':plan.shop_id,'offer_id':offer,'state':final['phase'],'product':record.get('product'),'stocks':record.get('stocks')})
            library.put(owner,p,{'channel':'plugin-production','offer_id':offer},connection=c)
            if record.get('verified'):
                modules={k:dict(c.execute('SELECT * FROM modules WHERE id=?',(v,)).fetchone()) for k,v in json.loads(wf['modules']).items()}
                c.execute('INSERT OR IGNORE INTO jobs(id,owner,source_key,store_id,phase,data,modules,next_at,created,updated,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (offer,owner,sku,store['id'],'selling',json.dumps({'candidate':review['candidate'],'match':match,'external_publication':record,'product_id':record['product']['product_id']}),json.dumps(modules),0,record['started_at'],time.time(),'插件→compareBot→毛子已回查可售：'+record['product']['sku']+'，库存99'))
        return {'sku':sku,'offer_id':offer,'phase':final['phase'],'verified':record.get('verified',False),'product':record.get('product'),
                'retryable_readback':final['phase']=='manual_review' and (final['details'].get('reason') in ('reconciliation_timeout','favorite_visibility_exhausted') or bool(final['details'].get('image_repair_dispatched_at'))),
                'reason':final['details'].get('reason'), 'favorite_recovery':final['details'].get('favorite_recovery'),
                'platform_issue_codes':final['details'].get('platform_issue_codes',[])}
