"""Same-product review independent of valuation and publication eligibility."""
import copy
import json
import math
import time
from .comparebot import screen
from .evaluation_requirements import review_sale_price as sale_price
from .source_library import fingerprint


def envelope(product):
    if not product.get('title') or not product.get('image'):
        raise ValueError('comparison_image_or_title_missing')
    detail=product.get('plugin_detail') or {}
    quote=sale_price(product)
    return {'source_key':str(product['sku']),'title':product['title'],'image':product['image'],
            'weight_g':detail.get('weight_g') or product.get('weight_g'),
            'sell_price_cny':quote['value'] if quote and quote['currency']=='CNY' else None,
            'origin':{'specifications':{'attributes':detail.get('attributes'),'variant_id':detail.get('variant_id')}}}


def comparison(report):
    e=(report.get('result') or {}).get('evidence') or {}
    return (e.get('source') or {}).get('comparebot') or e.get('comparebot') or {}


def bound(cb, candidate):
    q=(cb.get('search_and_rank') or {}).get('query') or {}
    return (str(q.get('product_id'))==candidate['source_key'] and q.get('title')==candidate['title']
            and q.get('image_url')==candidate['image']
            and q.get('specifications')==candidate['origin']['specifications'])


def verdict(cb):
    decision=cb.get('decision') or {};qwen=decision.get('qwen_review') or {}
    rows=(cb.get('search_and_rank') or {}).get('candidates') or []
    selected=[r for r in rows if str((r.get('candidate') or {}).get('offer_id'))==str(decision.get('selected_offer_id'))]
    if not rows and decision.get('reason')=='no_supplier_candidates':return 'no_candidates'
    if len(selected)!=1:return 'uncertain'
    score=selected[0].get('dinov2_similarity')
    if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not -1<=score<=1:return 'uncertain'
    if score<.63:return 'mismatch'
    size=(cb.get('search_and_rank') or {}).get('query',{}).get('size')
    if score>=.86 and size=='small':return 'match'
    if qwen.get('brand_or_model_conflict'):return 'mismatch'
    if qwen.get('verdict')=='match' and score>=.82:return 'match'
    if qwen.get('verdict')=='mismatch' and score<=.64:return 'mismatch'
    return 'uncertain'


def review_state(identity):
    return {'match':'same_product_confirmed','mismatch':'rejected'}.get(identity['verdict'],'needs_review')


def identity_result(cb, candidate, *, reused=False, observed_at=None):
    return {'contract':'same-product-review-v1','verdict':verdict(cb),'comparison':cb,
            'input_digest':fingerprint(candidate),'reviewed_at':time.time(),
            'evidence_observed_at':observed_at or time.time(),'reused_comparison':reused,
            'scope':'same_product_only','publication_authorized':False,
            'automatic_review':{'policy':'dino-qwen-v1','at':time.time()}}


async def evaluate(db,owner,sku,seller):
    key=(owner,sku,seller)
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0])
        queue=json.loads(c.execute('SELECT body FROM plugin_pipeline WHERE owner=? AND sku=? AND seller=?',key).fetchone()[0])
        prior=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        report=json.loads(prior[0]) if prior else {}
        workflow=c.execute("SELECT secrets FROM workflows WHERE owner=? AND json_extract(modules,'$.matcher')='flowb-matcher'",(owner,)).fetchone()
    candidate=envelope(p);cb=comparison(report)
    old_identity=report.get('identity_review') or {}
    if old_identity.get('comparison'):cb=old_identity['comparison']
    reusable=not queue.get('force_identity_review') and bound(cb,candidate) and bool((cb.get('decision') or {}).get('qwen_review') or verdict(cb) in ('match','mismatch','no_candidates'))
    if not reusable:
        raw=db.open(workflow[0]).get('flowb-matcher','') if workflow else ''
        keys=json.loads(raw) if raw.lstrip().startswith('{') else {}
        query=(cb.get('search_and_rank') or {}).get('query') or {}
        rank_matches=bool((cb.get('search_and_rank') or {}).get('candidates')) and (str(query.get('product_id'))==candidate['source_key'] and query.get('title')==candidate['title'] and query.get('image_url')==candidate['image'])
        if not rank_matches:cb=await screen(candidate)
        ranked=cb.get('search_and_rank')
        if ranked and ranked.get('candidates') and verdict(cb)!='mismatch':
            cb=await screen(candidate,keys.get('dashscope_api_key',''),ranking=ranked)
    identity=identity_result(cb,candidate,reused=reusable,observed_at=report.get('finished_at') if reusable else None)
    with db.write_transaction() as c:
        latest=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        if fingerprint(envelope(json.loads(latest[0])))!=fingerprint(candidate):raise ValueError('comparison_input_changed')
        latest_report=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone()
        saved=json.loads(latest_report[0]) if latest_report else {'sku':sku,'seller':seller,'state':'needs_review'}
        saved['identity_review']=identity
        saved['state']=review_state(identity)
        c.execute('INSERT INTO plugin_reviews VALUES(?,?,?,?,?,?) ON CONFLICT(owner,sku,seller) DO UPDATE SET state=excluded.state,body=excluded.body,updated=excluded.updated',
                  (*key,saved.get('state','needs_review'),json.dumps(saved),time.time()))
    return {'state':review_state(identity),'reason':'same_product_'+identity['verdict'],'identity_review':identity}


def reconcile_pending(c,key,queue):
    """Reuse conclusive identity evidence before a product enters manual review."""
    from .manual_reviews import publication_started
    if queue.get('human_review') or queue.get('force_identity_review') or publication_started(c,*key,queue):return None
    if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',key[:2]).fetchone():return None
    # A repair-only worker can run before the review module creates its tables.
    # Without saved evidence, keep the item in manual review.
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('plugin_reviews','sourcing_products')")}
    if tables != {'plugin_reviews','sourcing_products'}:return None
    row=c.execute('SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    product=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',key).fetchone()
    if not row or not product:return None
    report=json.loads(row[0]);old=report.get('identity_review') or {}
    if old.get('human_review'):return None
    try:candidate=envelope(json.loads(product[0]))
    except ValueError:return None
    cb=old.get('comparison') or comparison(report)
    if not bound(cb,candidate):return None
    identity=identity_result(cb,candidate,reused=True,observed_at=report.get('finished_at'))
    state=review_state(identity)
    if state=='needs_review':return None
    report.update(identity_review=identity,state=state)
    c.execute('UPDATE plugin_reviews SET state=?,body=?,updated=? WHERE owner=? AND sku=? AND seller=?',(state,json.dumps(report),time.time(),*key))
    queue.update(same_product_only=True,reason='same_product_'+identity['verdict'])
    return state


def confirm(report,actor,note):
    r=copy.deepcopy(report);identity=r.get('identity_review') or {}
    cb=identity.get('comparison') or comparison(r);dec=cb.get('decision') or {}
    rows=(cb.get('search_and_rank') or {}).get('candidates') or []
    selected=[x for x in rows if str((x.get('candidate') or {}).get('offer_id'))==str(dec.get('selected_offer_id'))]
    if len(selected)!=1 or not selected[0]['candidate'].get('image_url'):
        raise ValueError('缺少可核对的1688同款图片，请重新识别')
    if (dec.get('qwen_review') or {}).get('brand_or_model_conflict'):
        raise ValueError('存在品牌或型号冲突，不能确认同款')
    r['identity_review']={**identity,'contract':'same-product-review-v1','verdict':'match',
        'comparison':cb,'human_review':{'actor':actor,'note':note,'at':time.time()},
        'scope':'same_product_only','publication_authorized':False}
    return r


def card(c,owner,row,sku,seller,revision):
    r=json.loads(row['review'] or '{}');q=json.loads(row['queue'])
    saved=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
    p=json.loads(saved[0]) if saved else (r.get('candidate') or {}).get('origin') or {}
    identity=r.get('identity_review') or {};cb=identity.get('comparison') or comparison(r)
    dec=cb.get('decision') or {};rows=(cb.get('search_and_rank') or {}).get('candidates') or []
    selected=next((x for x in rows if str((x.get('candidate') or {}).get('offer_id'))==str(dec.get('selected_offer_id'))),{})
    offer=selected.get('candidate') or {};quote=sale_price(p)
    reason=None
    try:
        confirm(r,'preview','预览')
        if not bound(cb,envelope(p)):reason='商品对比资料已变化，请重新识别'
    except ValueError as error:reason=str(error)
    leased=c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,seller,time.time())).fetchone()
    blocked=c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone()
    if leased:reason='正在重新识别，请稍后刷新'
    if blocked:reason='商品在禁止处理清单中'
    outcome=identity.get('verdict','uncertain')
    label={'match':'已识别同款','mismatch':'已识别不同款','uncertain':'需核对同款','no_candidates':'未搜到候选货源'}[outcome]
    detail=(dec.get('qwen_review') or {}).get('reason') or '仅核对商品本体、款式和规格；价格与上架资料不影响同款结论。'
    if outcome=='no_candidates':detail='1688 图片搜索没有返回候选货源，尚不能判断是否存在同款，可重新搜索。'
    if identity.get('automatic_review') and outcome=='match':detail='已按 DINO／千问规则自动通过同款审核，可直接安排上架。'
    if identity.get('automatic_review') and outcome=='mismatch':detail='已按 DINO／千问规则自动淘汰。'
    if not identity:detail='尚未完成独立同款复核；可重新识别或核对两侧商品后确认。'
    repair_block='正在重新识别，请稍后刷新' if leased else '商品在禁止处理清单中' if blocked else None
    return {'sku':sku,'seller':seller,'revision':revision,'review_scope':'same_product_only',
      'category':'same_product','category_label':label,'identity_verdict':outcome,
      'title':p.get('title'),'image':p.get('image'),'url':p.get('url') or f'https://www.ozon.ru/product/{sku}/',
      'supplier_title':offer.get('title'),'supplier_image':offer.get('image_url'),'supplier_url':offer.get('offer_url'),
      'supplier_id':offer.get('offer_id'),'purchase':offer.get('price_cny'),
      'price_basis':quote,'sell_price_cny':quote['value'] if quote and quote['currency']=='CNY' else None,
      'profit_rate':None,'dino_score':selected.get('dinov2_similarity'),'qwen':dec.get('qwen_review') or {},
      'reason':detail,'missing_fields':[],'last_repair':{},'observed_at':identity.get('reviewed_at'),
      'can_approve':reason is None,'approval_block':reason,'can_repair':repair_block is None,'repair_block':repair_block,
      'can_reject':not leased,'allowed_actions':(['approve'] if reason is None else [])+(['repair'] if repair_block is None else [])+(['reject'] if not leased else [])}
