"""Tenant-scoped, revision-bound human review of the existing publication queue."""
import copy,json,time
from fastapi import Depends,HTTPException,Query
from pydantic import BaseModel,Field
from typing import Literal
from .source_library import fingerprint
from .pipeline_modules.admission import schema as queue_schema


def schema(db):
    queue_schema(db)
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS plugin_reviews(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))')
        c.execute('''CREATE TABLE IF NOT EXISTS human_reviews(id INTEGER PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,
          actor TEXT,action TEXT,note TEXT,revision TEXT,body TEXT,at REAL)''')


def load(c,owner,sku,seller):
    row=c.execute('''SELECT q.state,q.body queue,r.body review FROM plugin_pipeline q
      LEFT JOIN plugin_reviews r USING(owner,sku,seller) WHERE q.owner=? AND q.sku=? AND q.seller=?''',(owner,sku,seller)).fetchone()
    if not row:raise ValueError('商品不存在')
    return dict(row)


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


def card(c,owner,row,sku,seller):
    r=json.loads(row['review'] or '{}');q=json.loads(row['queue']);m=r.get('result') or {};e=m.get('evidence') or {};s=e.get('source') or {};candidate=r.get('candidate') or {}
    if not candidate:
        p=c.execute('SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller)).fetchone()
        candidate=json.loads(p[0]) if p else {}
    decision=(s.get('comparebot') or {}).get('decision') or {};profit=e.get('profit') or {}
    reason=None
    try:approval(c,owner,sku,seller,r,'preview','预览')
    except ValueError as error:reason=str(error)
    if (q.get('repair_retry') or {}).get('exhausted_at'):reason='补资料重试耗尽，请先补资料并重新测算'
    if q.get('submitted'):reason='已提交平台，请在发布异常流程处理'
    return {'sku':sku,'seller':seller,'revision':revision(row),'title':candidate.get('title'),'image':candidate.get('image'),
      'url':(candidate.get('origin') or {}).get('url') or f'https://www.ozon.ru/product/{sku}/',
      'supplier_title':s.get('selected_title'),'supplier_image':s.get('selected_image_url'),'supplier_url':s.get('selected_offer_url'),
      'supplier_id':s.get('selected_offer_id'),'purchase':s.get('selected_cost_cny'),
      'profit_rate':(profit.get('assessment') or {}).get('erp_profit_rate_pct'),'sell_price_cny':profit.get('sell_price_cny'),
      'dino_score':(s.get('selected_offer_image') or {}).get('score'),'qwen':decision.get('qwen_review') or {},
      'reason':q.get('reason') or q.get('error') or m.get('reason') or r.get('reason'),
      'observed_at':r.get('finished_at'),'can_approve':reason is None,'approval_block':reason}


def decide(db,owner,actor,sku,seller,action,note,expected):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE');row=load(c,owner,sku,seller)
        if row['state']!='needs_review' or revision(row)!=expected:raise ValueError('状态或测算结果已变化，请刷新后再审核')
        if c.execute('SELECT 1 FROM plugin_pipeline_leases WHERE owner=? AND sku=? AND seller=? AND expires>?',(owner,sku,seller,time.time())).fetchone():raise ValueError('商品正在处理中，请稍后刷新')
        r=json.loads(row['review'] or '{}');q=json.loads(row['queue']);now=time.time()
        if q.get('submitted'):raise ValueError('商品已提交平台，请在发布异常流程处理，不能重复提交')
        if action=='approve':
            if (q.get('repair_retry') or {}).get('exhausted_at'):raise ValueError('补资料重试耗尽，请先补资料并重新测算')
            if c.execute('SELECT 1 FROM blocks WHERE owner=? AND source_key=?',(owner,sku)).fetchone():raise ValueError('商品在禁止上架清单中')
            r=approval(c,owner,sku,seller,r,actor,note);state='publishing'
            c.execute('UPDATE plugin_reviews SET state=?,body=? WHERE owner=? AND sku=? AND seller=?',('matched',json.dumps(r),owner,sku,seller))
        elif action=='reject':
            state='rejected'
            c.execute("UPDATE plugin_reviews SET state='rejected' WHERE owner=? AND sku=? AND seller=?",(owner,sku,seller))
        elif action=='repair':
            state='needs_fields'
            if q.get('repair_retry'):
                q.setdefault('repair_history',[]).append(q.pop('repair_retry'))
            # Audit below retains the exact old report before cache invalidation.
            c.execute('DELETE FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?',(owner,sku,seller))
        else:raise ValueError('未知审核操作')
        c.execute('INSERT INTO human_reviews(owner,sku,seller,actor,action,note,revision,body,at) VALUES(?,?,?,?,?,?,?,?,?)',
            (owner,sku,seller,actor,action,note,expected,json.dumps(row),now))
        q.update(human_review={'actor':actor,'action':action,'note':note,'at':now},reason='human_'+action)
        q.pop('error',None)
        c.execute('UPDATE plugin_pipeline SET state=?,body=?,due=0,attempts=0 WHERE owner=? AND sku=? AND seller=?',
            (state,json.dumps(q),owner,sku,seller))
        return {'state':state,'action':action}


def register(app,db,scope,auth):
    schema(db)
    class Decision(BaseModel):
        action:Literal['approve','reject','repair']
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
