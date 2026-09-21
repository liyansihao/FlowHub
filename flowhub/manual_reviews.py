"""Tenant-scoped, revision-bound human review of the existing publication queue."""
import copy,json,time
from fastapi import Depends,HTTPException,Query
from pydantic import BaseModel,Field
from typing import Literal
from .source_library import fingerprint
from .pipeline_modules.admission import schema as queue_schema


def schema(db):
    queue_schema(db)
    from .listing_controls import schema as listing_schema
    listing_schema(db)
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_reviews(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))')
        c.execute('''CREATE TABLE IF NOT EXISTS human_reviews(id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,
          actor TEXT,action TEXT,note TEXT,revision TEXT,body TEXT,at REAL)''')


def load(c,owner,sku,seller):
    row=c.execute('''SELECT q.state,q.body queue,r.body review FROM plugin_pipeline q
      LEFT JOIN plugin_reviews r USING(owner,sku,seller) WHERE q.owner=? AND q.sku=? AND q.seller=?''',(owner,sku,seller)).fetchone()
    if not row:raise ValueError('商品不存在')
    result=dict(row)
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='product_listing_controls'").fetchone():
        control=c.execute('SELECT action,state,body,updated FROM product_listing_controls WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        result['listing_control']=dict(control) if control else None
    return result


def revision(row):return fingerprint(row)


def matched(review,actor,note):
    r=copy.deepcopy(review);result=r.get('result') or {};evidence=result.get('evidence') or {};source=evidence.get('source') or {}
    decision=(source.get('comparebot') or {}).get('decision') or {}
    if r.get('state')!='needs_review' or not result.get('manual_review') or not str(result.get('reason','')).startswith('qwen_'):
        raise ValueError('此任务不是可人工确认的同款复核，请先补资料或重新测算')
    if (decision.get('qwen_review') or {}).get('brand_or_model_conflict'):
        raise ValueError('存在品牌或型号冲突，不能人工放行')
    if not source.get('selected_offer_id') or not source.get('selected_cost_cny'):
        raise ValueError('缺少绑定的1688商品或采购成本')
    source['automated_decision']=copy.deepcopy(decision)
    decision.update(outcome='approved',reason='human_same_product_confirmation',human_review={'actor':actor,'note':note,'at':time.time()})
    source['evaluation_only']=False
    result.update(manual_review=False,rejected=False,reason='human_same_product_confirmation',supplier_id=str(source['selected_offer_id']),
                  purchase=source['selected_cost_cny'])
    r['state']='matched';r['human_review']=decision['human_review']
    # Do not refresh finished_at: old prices and facts must still expire.
    return r


def approval(c,owner,sku,seller,review,actor,note):
    from .plugin_publication import approved
    wf=c.execute('SELECT rules FROM workflows WHERE owner=?',(owner,)).fetchone()
    if not wf:raise ValueError('工作流不存在')
    rules=json.loads(wf[0]);r=matched(review,actor,note)
    permission=c.execute('SELECT expires FROM plugin_publication_permissions WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
    allowed=bool(permission and permission[0]>time.time())
    try:approved(r,rules,allow_unknown=allowed)
    except (KeyError,TypeError,ValueError) as e:raise ValueError('需补资料或重新测算：'+str(e)) from None
    return r



FIELD_LABELS = {
    'title':'商品标题', 'image':'商品主图', 'url':'商品链接', 'weight_g':'包装重量',
    'dimensions_mm':'包装长宽高', 'attributes':'上架商品属性', 'fresh_dossier':'六小时内的商品资料',
    'sale_price':'有效售价', 'publication_attributes_missing':'上架商品属性',
}
REASON_LABELS = {
    'valuation_inputs_ready':'测算基础数据已具备',
    'complete_dossier':'商品档案已补齐',
    'dossier_synced_to_manual_review':'商品档案已更新，保留原人工同款复核',
    'dossier_synced_to_approved_review':'商品档案已更新，保留原有效测算',
    'fresh_comparebot_approval_required':'同款与利润测算已过期或尚未完成，需要重新测算',
    'price_evidence_stale':'售价依据已过期，需要更新价格并重新测算',
    'plugin_facts_missing_or_stale':'商品资料缺失或过期，需要重新采集',
    'pricing_facts_missing':'利润测算所需的价格、重量或尺寸缺失',
    'fresh_pure_fbs_required':'缺少有效的纯 FBS 发货方式证明',
    'follow_permission_unverified_or_blocked':'跟卖许可尚未核实或卖家禁止跟卖',
    'explicit_source_restriction':'商品存在明确的发货方式或跟卖限制',
    'profit_below_workflow_threshold':'成本利润率未达到工作流门槛',
    'supplier_cost_binding_mismatch':'1688 货源与采购成本绑定不一致',
    'qwen_match_in_safe_band':'千问判定同款且 DINO 相似度 ≥82%，仍需通过利润与上架检查',
    'qwen_match_outside_safe_band':'千问判定同款，但 DINO 相似度未达到82%的自动同款门槛，需人工复核',
    'qwen_mismatch_outside_safe_band':'千问判定不同款，但图像相似度存在分歧，需人工复核',
    'qwen_explicit_brand_or_model_conflict':'千问识别到品牌或型号冲突，不能人工放行',
    'qwen_uncertain_outside_safe_band':'同款识别不确定，需要人工核对商品本体和规格',
    'qwen_not_configured':'千问同款识别尚未配置',
    'qwen_billing_blocked':'千问余额或计费受限',
    'qwen_authentication_blocked':'千问鉴权失败',
    'qwen_request_failed':'千问同款识别请求失败',
    'qwen_rate_limited':'千问同款识别请求限流',
    'maozi_collection_box_full':'毛子 ERP 采集箱已满，需要清理容量后重试',
    'hour_window_closed':'本次上架授权时间窗口已结束',
    'target_quota_unavailable':'目标店铺商品容量或当日上架额度不足',
    'reconciliation_timeout':'平台结果回查超时，需要核对原发布记录',
    'awaiting_exact_remote_outcome':'等待平台返回该商品的准确发布结果',
    'warning_all_image_failed':'平台提示全部商品图片处理失败',
    'DESCRIPTION_DECLINE':'平台拒绝商品描述，需要检查平台审核详情',
    'publication_dossier_pending':'上架资料尚未补齐',
    'source_identity_changed':'来源 SKU 或卖家与审核记录不一致，需要重新核对',
    'source_comparison_changed':'来源标题或图片已变化，需要重新核对同款',
    'source_package_or_attributes_changed':'包装或商品属性已变化，需要重新核对资料和测算',
    'current_source_packet_missing':'缺少当前来源记录，需要恢复来源证据',
    'product_blocked':'商品在禁止上架清单中',
    'explicit_delist':'商品在明确下架清单中',
}


def chinese_reason(value):
    text=str(value or '')
    for code,label in sorted((REASON_LABELS | FIELD_LABELS).items(),key=lambda x:-len(x[0])):
        text=text.replace(code,label)
    text=text.replace('repair_retry_exhausted:', '自动补资料重试已耗尽：').replace('publication_checks_required:', '上架检查未通过：').replace('missing:', '缺少：')
    if text and not any('\u4e00'<=ch<='\u9fff' for ch in text):
        return '流程检查未通过，需核对处理记录（'+text+'）'
    return text



def repair_details(product):
    repair=((product or {}).get('collection_evidence') or {}).get('last_repair') or {}
    failures=[]
    known={
        'favorite creation not confirmed; lookup again later':'收藏创建尚未确认，需回查原收藏，不能重复创建',
        'source favorite missing; a real price is required before creating one':'未找到已有收藏，需要有效售价后才能创建收藏',
        'favorite recovery listing incomplete':'收藏列表未完整返回，暂不能确认收藏是否存在',
        'favorite recovery listing changed; lookup again later':'收藏列表发生变化，需要重新回查',
        'SOURCE_REQUEST_FAILED':'毛子 ERP 补采请求失败',
    }
    sources={'maozi-draft':'毛子草稿补采','maozi-sku3':'毛子 sku3 价格更新','price':'毛子 sku3 请求','category':'毛子类目资料','ozon-seller-direct':'官方商品资料'}
    for step in repair.get('steps') or []:
        if step.get('source')=='maozi-sku3-detail' or step.get('reason')=='no_verified_package_dossier_fields':continue
        diagnostic=step.get('diagnostic') or {};code=str(step.get('reason') or '')
        message=str(diagnostic.get('api_message') or '')[:500]
        pending=(step.get('status') or {}).get('update_sales')
        from .plugin_detail import positive
        if pending is True:
            explanation='sku3 销量或价格数据仍待更新，尚不能用作有效售价依据'
        elif step.get('source')=='maozi-sku3' and (not step.get('seller_id') or str(step.get('seller_id'))!=str((product or {}).get('seller_id')) or not positive(step.get('price'))):
            explanation='毛子未返回匹配卖家的有效售价'
        elif message:
            if '采集箱已满' in message:explanation='毛子 ERP 采集箱容量已满：'+message
            elif '收藏数量已达上限' in message:explanation='毛子 ERP 收藏容量已达上限：'+message
            else:explanation=chinese_reason(message)
        elif code in known:explanation=known[code]
        elif code and code not in ('identity_fields_present','exact_source_identity_read','official_credentials_unavailable','exact_source_not_in_authorized_catalog'):
            explanation=chinese_reason(code)
        elif step.get('ok') is False:explanation='补采请求失败，尚未取得有效资料'
        else:continue
        failures.append({'source':sources.get(step.get('source'),step.get('source') or '资料补采'),
                         'reason':explanation,'reason_code':code})
    return {'at':repair.get('at'),'failures':failures}

def publication_started(c,owner,sku,seller,q):
    # A saved publication plan must be reconciled, never recreated by repair.
    if q.get('submitted') or q.get('offer_id') or q.get('phase') in ('submitting','reconciling','sync_pending','stock_ready','stock_pending','stock_verified','manual_review','failed'):
        return True
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone():
        return bool(c.execute('SELECT 1 FROM plugin_publications WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone())
    return False


def classification(q,r,started=False,current_missing=None):
    result=r.get('result') or {};retry=q.get('repair_retry') or {}
    codes=[q.get('reason'),q.get('error'),q.get('repair_reason'),retry.get('reason'),result.get('reason'),r.get('reason'),*(r.get('publication_blockers') or []),*(q.get('platform_issue_codes') or [])]
    codes=list(dict.fromkeys(str(x) for x in codes if x))
    missing=list(dict.fromkeys(current_missing if current_missing is not None else [*(retry.get('missing_fields') or []),*(q.get('pending_publication_fields') or [])]))
    reasons=[chinese_reason(x) for x in codes]
    if current_missing is not None:
        reasons=[('历史队列记录：'+chinese_reason(x)) if x in (q.get('reason'),q.get('repair_reason'),retry.get('reason')) and ('missing' in x or 'repair_retry_exhausted' in x) else chinese_reason(x) for x in codes]
        if not missing:reasons.append('最新来源资料已补齐；批准仍以审核报告内的同款、测算和上架检查为准')
    if missing:reasons.append('待补资料：'+'、'.join(FIELD_LABELS.get(x,x) for x in missing))
    expired=any(x in ' '.join(codes) for x in ('price_evidence_stale','fresh_comparebot_approval_required'))
    stamp=r.get('finished_at')
    expired=expired or (isinstance(stamp,(int,float)) and not 0<=time.time()-stamp<21600)
    if started:kind,label='publication_error','发布异常';reasons.insert(0,'已有平台发布记录，需回查原记录；不能从审核台重复提交或补资料重上架')
    elif expired:kind,label='stale_evaluation','测算过期';reasons.insert(0,'测算或价格依据已过期，需要补齐有效资料后重新测算')
    elif missing or any('missing' in x for x in (r.get('publication_blockers') or [])) or (current_missing is None and (retry.get('exhausted_at') or any(x in ' '.join(codes) for x in ('missing','publication_dossier_pending','plugin_facts_missing_or_stale')))):
        kind,label='missing_fields','缺资料'
    elif r.get('state')=='needs_review' and result.get('manual_review') and str(result.get('reason','')).startswith('qwen_'):
        kind,label='same_product','人工同款复核'
    elif not r:kind,label='missing_fields','缺资料';reasons.insert(0,'缺少测算报告，需要补齐资料后重新测算')
    else:kind,label='publication_error','发布异常'
    return {'category':kind,'category_label':label,'reason_codes':codes,'reasons':reasons or ['缺少具体处理原因，请核对本地流程记录'],'missing_fields':missing}

def card(c,owner,row,sku,seller):
    if json.loads(row['queue']).get('same_product_only') and not publication_started(c,owner,sku,seller,json.loads(row['queue'])):
        from .identity_review import card as identity_card
        return identity_card(c,owner,row,sku,seller,revision(row))
    r=json.loads(row['review'] or '{}');q=json.loads(row['queue']);m=r.get('result') or {};e=m.get('evidence') or {};s=e.get('source') or {};candidate=r.get('candidate') or {}
    p=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
    current_source=json.loads(p[0]) if p else None
    current_missing=None
    if current_source is not None:
        from .pipeline_modules.repair import missing_fields
        current_missing=missing_fields(current_source)
    if not candidate:candidate=current_source or {}
    decision=(s.get('comparebot') or {}).get('decision') or {};profit=e.get('profit') or {}
    reason=None
    try:approval(c,owner,sku,seller,r,'preview','预览')
    except ValueError as error:reason=str(error)
    started=publication_started(c,owner,sku,seller,q)
    display_review=r
    permission=c.execute('SELECT expires FROM plugin_publication_permissions WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
    if permission and permission[0]>time.time():
        from .plugin_publication import unknown_restrictions_allowed
        if unknown_restrictions_allowed(r):
            display_review={**r,'publication_blockers':[b for b in r.get('publication_blockers',[]) if b not in ('fresh_pure_fbs_required','follow_permission_unverified_or_blocked')]}
    info=classification(q,display_review,started,current_missing)
    if (q.get('repair_retry') or {}).get('exhausted_at'):reason='补资料重试耗尽，请先补资料并重新测算'
    if started:reason='已提交平台或已有发布记录，请在发布异常流程处理'
    blocked=bool(c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone())
    leased=bool(c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,seller,time.time())).fetchone())
    if blocked:reason='商品在禁止上架清单中'
    if leased:reason='商品正在处理中，请稍后刷新'
    repair_block='商品已提交平台或已有发布记录，请回查原发布结果' if started else '商品在禁止上架清单中' if blocked else '商品正在处理中，请稍后刷新' if leased else None
    if row['state']!='needs_review':reason=repair_block='状态已变化，请刷新后再审核'
    can_reject=row['state']=='needs_review' and not started and not leased
    return {**info,'last_repair':repair_details(current_source),'sku':sku,'seller':seller,'revision':revision(row),'title':candidate.get('title'),'image':candidate.get('image'),
      'url':(candidate.get('origin') or {}).get('url') or f'https://www.ozon.ru/product/{sku}/',
      'supplier_title':s.get('selected_title'),'supplier_image':s.get('selected_image_url'),'supplier_url':s.get('selected_offer_url'),
      'supplier_id':s.get('selected_offer_id'),'purchase':s.get('selected_cost_cny'),
      'profit_rate':(profit.get('assessment') or {}).get('erp_profit_rate_pct'),'sell_price_cny':profit.get('sell_price_cny'),
      'dino_score':(s.get('selected_offer_image') or {}).get('score'),'qwen':decision.get('qwen_review') or {},
      'reason':'；'.join(info['reasons']),
      'observed_at':r.get('finished_at'),'can_approve':reason is None,'approval_block':chinese_reason(reason) if reason else None,
      'can_repair':repair_block is None,'repair_block':repair_block,'can_reject':can_reject,
      'allowed_actions':(['approve'] if reason is None else [])+(['repair'] if repair_block is None else [])+(['reject'] if can_reject else [])}


def decide(db,owner,actor,sku,seller,action,note,expected,*,replay=False,review_scope=None):
    if seller.startswith('owned-audit:'):
        from .sellable_audit_policy import decide as audit_decide
        return audit_decide(db,owner,actor,sku,seller,action,note,expected,replay=replay)
    if not isinstance(note,str) or not note.strip() or len(note)>1000:raise ValueError('请填写1至1000字的审核理由')
    note=note.strip()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        prior=c.execute('SELECT body FROM human_reviews WHERE owner=? AND sku=? AND seller=? AND actor=? AND action=? AND note=? AND revision=?',(owner,sku,seller,actor,action,note,expected)).fetchone() if replay else None
        if prior:
            identity_only=json.loads(json.loads(prior[0])['queue']).get('same_product_only')
            states={'approve':'same_product_confirmed' if identity_only else 'publishing','repair':'queued' if identity_only else 'needs_fields','reject':'rejected','list':'queued','unlist':'queued'}
            return {'state':states[action],'action':action,'replayed':True}
        row=load(c,owner,sku,seller)
        if action in ('list','unlist'):
            if revision(row)!=expected:raise ValueError('商品状态已变化，请刷新后操作')
            if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,seller,time.time())).fetchone():raise ValueError('商品正在处理中，请稍后操作')
            from .listing_controls import request as listing_request
            outcome=listing_request(c,owner,sku,seller,action,actor,note)
            c.execute('INSERT INTO human_reviews(owner,sku,seller,actor,action,note,revision,body,at) VALUES(?,?,?,?,?,?,?,?,?)',(owner,sku,seller,actor,action,note,expected,json.dumps(row),time.time()))
            if action=='unlist':
                c.execute("UPDATE plugin_pipeline SET state='delisting' WHERE owner=? AND sku=? AND seller=?",(owner,sku,seller))
                c.execute('INSERT OR IGNORE INTO blocks VALUES(?,?,?)',(owner,sku,'website_unlist'))
            else:
                c.execute("DELETE FROM blocks WHERE owner=? AND source_key=? AND reason='website_unlist'",(owner,sku))
            return outcome
        if row['state']!='needs_review' or revision(row)!=expected:raise ValueError('状态或测算结果已变化，请刷新后再审核')
        if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,seller,time.time())).fetchone():raise ValueError('商品正在处理中，请稍后刷新')
        r=json.loads(row['review'] or '{}');q=json.loads(row['queue']);now=time.time()
        if review_scope=='same_product_only':
            q['same_product_only']=True
            row={**row,'queue':json.dumps(q)}
        if publication_started(c,owner,sku,seller,q):raise ValueError('商品已提交平台，请在发布异常流程处理，不能重复提交')
        if action=='approve':
            if not q.get('same_product_only') and (q.get('repair_retry') or {}).get('exhausted_at'):raise ValueError('补资料重试耗尽，请先补资料并重新测算')
            if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone():raise ValueError('商品在禁止上架清单中')
            if q.get('same_product_only'):
                from .identity_review import confirm
                from .identity_review import envelope,bound,comparison
                current=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
                cb=(r.get('identity_review') or {}).get('comparison') or comparison(r)
                if not current or not bound(cb,envelope(json.loads(current[0]))):raise ValueError('商品对比资料已变化，请重新识别')
                r=confirm(r,actor,note);state='same_product_confirmed'
            else:r=approval(c,owner,sku,seller,r,actor,note);state='publishing'
            c.execute('UPDATE plugin_reviews SET state=?,body=? WHERE owner=? AND sku=? AND seller=?',('matched',json.dumps(r),owner,sku,seller))
        elif action=='reject':
            state='rejected'
            c.execute("UPDATE plugin_reviews SET state='rejected' WHERE owner=? AND sku=? AND seller=?",(owner,sku,seller))
        elif action=='repair':
            if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone():raise ValueError('商品在禁止上架清单中，不能修复重上架')
            state='queued' if q.get('same_product_only') else 'needs_fields'
            if q.get('same_product_only'):
                r.pop('identity_review',None)
                q['force_identity_review']=True
                c.execute('UPDATE plugin_reviews SET body=? WHERE owner=? AND sku=? AND seller=?',(json.dumps(r),owner,sku,seller))
            else:q['repair_full_dossier']=True
            if q.get('repair_retry'):
                q.setdefault('repair_history',[]).append(q.pop('repair_retry'))
            # Keep the exact report for the repair lane to compare identity/economics.
            # needs_fields + full dossier never grants or renews approval here.
        else:raise ValueError('未知审核操作')
        c.execute('INSERT INTO human_reviews(owner,sku,seller,actor,action,note,revision,body,at) VALUES(?,?,?,?,?,?,?,?,?)',
            (owner,sku,seller,actor,action,note,expected,json.dumps(row),now))
        if q.get('same_product_only') and action in ('approve','reject'):
            from .listing_controls import request as listing_request
            listing_request(c,owner,sku,seller,'list' if action=='approve' else 'unlist',actor,note)
        q.update(human_review={'actor':actor,'action':action,'note':note,'at':now},reason='human_'+action)
        q.pop('error',None)
        c.execute('UPDATE plugin_pipeline SET state=?,body=?,due=0,attempts=0 WHERE owner=? AND sku=? AND seller=?',
            (state,json.dumps(q),owner,sku,seller))
        return {'state':state,'action':action}


def register(app,db,scope,auth):
    schema(db)
    class Decision(BaseModel):
        action:Literal['approve','reject','repair','list','unlist']
        note:str=Field(min_length=1,max_length=1000)
        revision:str=Field(min_length=64,max_length=64)

    @app.get('/api/manual-reviews')
    def listing(owner=Depends(scope),offset:int=Query(0,ge=0),search:str=Query('',max_length=100)):
        with db.connect() as c:
            args=(owner,'%'+search+'%')
            count=c.execute("SELECT COUNT(*) FROM plugin_pipeline WHERE owner=? AND state='needs_review' AND sku LIKE ?",args).fetchone()[0]
            rows=c.execute("SELECT sku,seller FROM plugin_pipeline WHERE owner=? AND state='needs_review' AND sku LIKE ? ORDER BY sku,seller LIMIT 20 OFFSET ?",(*args,offset)).fetchall()
            return {'total':count,'items':[card(c,owner,load(c,owner,r['sku'],r['seller']),r['sku'],r['seller']) for r in rows]}

    @app.get('/api/manual-reviews/history')
    def history(owner=Depends(scope)):
        with db.connect() as c:
            return [dict(r) for r in c.execute('SELECT sku,seller,actor,action,note,at FROM human_reviews WHERE owner=? ORDER BY id DESC LIMIT 50',(owner,))]

    @app.post('/api/manual-reviews/{sku}/{seller}')
    def action(sku:str,seller:str,payload:Decision,owner=Depends(scope),user=Depends(auth)):
        if not payload.note.strip():raise HTTPException(422,'请填写审核理由')
        try:return decide(db,owner,user['id'],sku,seller,payload.action,payload.note.strip(),payload.revision)
        except ValueError as e:raise HTTPException(409,str(e)) from None
