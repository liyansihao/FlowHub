const $=s=>document.querySelector(s);
let snapshot={items:[],history:[],pages:1},page=0,view='queue',controller,sequence=0,searchTimer;
const drafts=new Map(),submitting=new Set();
let submissionRefreshTimer;
let displayedQuery='',displayedEtag='',pollTimer,quietDelay=30000;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const key=r=>`${r.sku}::${r.seller}`;
const labels={queued:'等待处理',running:'处理中',waiting:'等待平台回查',listed:'已回查上架',unlisted:'已回查下架',no_listing:'未找到本店发布记录',blocked:'处理异常',selling:'已上架',delisted:'已下架',not_listed:'未上架',needs_review:'待人工审核',same_product_confirmed:'同款审核已通过',pending:'等待本机同步',applied:'本机已处理',rejected:'未通过执行校验'};
const label=s=>labels[s]||'处理中';
const date=v=>{if(!v)return '暂无时间';const d=new Date(typeof v==='string'&&v.includes('T')?v:Number(v)*1000);return Number.isNaN(d.getTime())?'暂无时间':d.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});};
const pct=v=>v!=null&&Number.isFinite(Number(v))?`${Math.round(Number(v)*100)}%`:'—';
function url(v){try{const u=new URL(v);return u.protocol==='https:'?u.href:'';}catch{return '';}}
function link(v,text){return url(v)?`<a href="${esc(url(v))}" target="_blank" rel="noopener noreferrer">${text} ↗</a>`:'';}
function media(v){return url(v)?`<img src="${esc(url(v))}" alt="商品对比图片" loading="lazy" referrerpolicy="no-referrer"><div class="no-image" hidden>图片暂不可用，请打开原商品查看</div>`:'<div class="no-image">暂无图片</div>';}
function enabled(r,a){return !submitting.has(key(r))&&!['pending','applied'].includes(r.decision?.status)&&r.allowed_actions?.includes(a);}
// A pending review uses the same two lifecycle entry points. Reviewed products keep direct controls.
function actionFor(r,direction){const review=direction==='list'?'approve':'reject';return r.pipeline_state==='needs_review'&&r.allowed_actions?.includes(review)?review:direction;}
async function api(method='GET',body,signal,{conditional=false}={}){
 const query=new URLSearchParams({page:String(page),page_size:'12',view,q:$('#search').value.trim(),state:$('#category').value});
 const queryKey=query.toString(),headers={'content-type':'application/json'};
 if(method==='GET'&&conditional&&queryKey===displayedQuery&&displayedEtag)headers['if-none-match']=displayedEtag;
 const res=await fetch(`/api/reviews${method==='GET'?'?'+query:''}`,{method,signal,headers,body:body?JSON.stringify(body):undefined});
 if(res.status===304)return {notModified:true};
 let data;try{data=await res.json();}catch{throw Error(`服务返回异常（${res.status}），请重新加载`);}
 if(!res.ok)throw Error(data.error||`请求失败（${res.status}）`);
 if(method==='GET'&&(!Array.isArray(data.items)||!Array.isArray(data.history)))throw Error('商品数据格式异常，请重新加载');
 if(method==='GET')data._cache={query:queryKey,etag:res.headers.get('etag')||''};
 else{displayedQuery='';displayedEtag='';}
 return data;
}
function remember(){document.querySelectorAll('.review textarea').forEach(el=>drafts.set(el.closest('.review').dataset.id,el.value));}
function schedulePoll(){clearTimeout(pollTimer);if(!document.hidden)pollTimer=setTimeout(()=>load({quiet:true}),quietDelay);}
async function load({quiet=false}={}){
 if(submitting.size){schedulePoll();return;}
 if(quiet&&(controller||document.hidden||document.activeElement?.closest('.review'))){schedulePoll();return;}
 clearTimeout(pollTimer);if(!quiet)quietDelay=30000;
 controller?.abort();const active=new AbortController();controller=active;const version=++sequence;
 const timeout=setTimeout(()=>active.abort(),25000);
 $('#sync-state').textContent='正在同步…';$('#list').setAttribute('aria-busy','true');
 try{const data=await api('GET',undefined,active.signal,{conditional:quiet});if(version!==sequence)return;if(data.notModified){quietDelay=Math.min(quietDelay*2,300000);}else{quietDelay=30000;remember();snapshot=data;displayedQuery=data._cache.query;displayedEtag=data._cache.etag;page=data.page||0;render();}$('#error').hidden=true;$('#sync-state').textContent='已连接';}
 catch(error){if(version!==sequence)return;quietDelay=Math.min(quietDelay*2,300000);$('#sync-state').textContent='连接中断';$('#error').hidden=false;$('#error span').textContent=error.name==='AbortError'?'加载超时，请重试。已加载的商品仍保留。':error.message;if(!snapshot.items.length&&!snapshot.history.length)$('#list').innerHTML='<div class="empty">暂时无法加载，请点击「重新加载」。</div>';}
 finally{clearTimeout(timeout);if(version===sequence){controller=null;$('#list').setAttribute('aria-busy','false');schedulePoll();}}
}
function render(){
 $('#total').textContent=snapshot.queue_total??0;$('#snapshot-time').textContent=`数据更新 ${date(snapshot.generated_at)}`;
 $('#page-info').textContent=`共 ${snapshot.filtered_total??0} ${view==='history'?'条记录':'件商品'} · 第 ${page+1} / ${snapshot.pages||1} 页`;
 $('#prev').disabled=page===0;$('#next').disabled=page+1>=(snapshot.pages||1);
 $('#products').setAttribute('aria-pressed',String(view==='queue'));$('#all').setAttribute('aria-pressed',String(view==='products'));$('#history').setAttribute('aria-pressed',String(view==='history'));$('#category').disabled=view!=='products';
 if(view==='history'){$('#list').innerHTML=snapshot.history.length?snapshot.history.map(r=>`<article class="history-row"><strong>${esc(r.sku)} · ${esc(({approve:'确认同款并安排上架',reject:'确认不同款并安排下架',repair:'重新识别',list:'上架',unlist:'下架'})[r.action]||r.action)}</strong><small>${esc(r.reviewer||'审核人')} · ${date(r.created_at)} · ${esc(label(r.status))}</small><p>${esc(r.note)} ${esc(r.last_error||'')}</p>${r.listing_state?`<p>平台处理：${esc(label(r.listing_state))}${r.target_store_name?' · '+esc(r.target_store_name):''} ${esc(r.listing_error||'')}</p>`:''}</article>`).join(''):'<div class="empty">暂无匹配的操作记录</div>';return;}
 $('#list').innerHTML=snapshot.items.length?snapshot.items.map(r=>{
 const up=actionFor(r,'list'),down=actionFor(r,'unlist');const price=r.price_basis;
 return `<article class="review" data-id="${esc(key(r))}"><div class="visuals"><div class="visual"><div class="visual-label"><span>OZON 商品</span>${link(r.url,'查看')}</div>${media(r.image)}</div><div class="visual"><div class="visual-label"><span>1688 货源</span>${link(r.supplier_url,'查看')}</div>${media(r.supplier_image)}<div class="supplier-title">${esc(r.supplier_title||'暂无货源标题')}</div></div></div><div class="details"><div class="detail-head"><h2>${esc(r.title||'暂无商品标题')}</h2><span class="badge">${esc(r.pipeline_state==='rejected'?'已淘汰':label(r.pipeline_state))}</span></div><p class="meta">SKU ${esc(r.sku)} · 来源卖家 ${esc(r.seller)}<br>目标店铺 ${esc(r.target_store_name||'待核对')}</p><div class="metrics"><div><span>${price?.kind==='minimum_follow_price'?'最低跟卖价':'普通售价'}</span><strong>${price&&Number.isFinite(Number(price.value))?esc((price.currency==='RUB'?'₽':'¥')+Number(price.value).toFixed(2)):'暂无'}</strong> <small>${price?.historical?'历史参考':''}</small></div><div><span>图片相似度</span><strong>${pct(r.dino_score)}</strong></div><div><span>千问判断 · 置信度</span><strong>${esc(({match:'同款',mismatch:'不同款',uncertain:'待确认'})[r.qwen?.verdict]||'暂无')} · ${pct(r.qwen?.confidence)}</strong></div></div><p class="reason">${esc(r.qwen?.reason||r.reason||'请对照图片、款式和规格判断是否同款。')}</p>${r.listing_state?`<div class="progress ${r.listing_state==='blocked'?'error':''}"><b>${esc(label(r.listing_state))}</b>${r.listing_error?` · ${esc(r.listing_error)}`:''}</div>`:''}${r.decision?`<div class="progress">${esc(label(r.decision.status))}${r.decision.last_error?' · '+esc(r.decision.last_error):''}</div>`:''}<details><summary>查看补充信息与限制</summary>${(r.store_switches||[]).map(x=>`<p>原店额度不足：${esc(x.from_store_name)} → ${esc(x.to_store_name)} · ${date(x.at)}</p>`).join('')}<p>价格采集：${date(price?.observed_at)} · 同款复核：${date(r.observed_at)}</p><p>模型置信度仅供参考，审核以款式和规格是否一致为准。</p>${[r.approval_block,r.repair_block,...(r.last_repair?.failures||[]).map(x=>x.reason)].filter(Boolean).map(x=>`<p>${esc(x)}</p>`).join('')}</details><div class="decision"><textarea aria-label="处理理由" maxlength="1000" placeholder="处理理由（选填，可补充规格差异）">${esc(drafts.get(key(r))||'')}</textarea><div class="actions"><button class="primary" data-action="${up}" ${enabled(r,up)?'':'disabled'}>${up==='approve'?'确认同款并上架':'上架'}</button><button data-action="${down}" ${enabled(r,down)?'':'disabled'}>${down==='reject'?'确认不同款并下架':'下架'}</button>${r.allowed_actions?.includes('repair')?`<button class="repair" data-action="repair" ${enabled(r,'repair')?'':'disabled'}>重新识别</button>`:''}</div><p class="action-hint">${up==='approve'||down==='reject'?'点击即提交同款判断及对应上下架操作。':'上下架操作将提交至处理队列。'}处理期间请等待平台回查。</p></div></div></article>`;
 }).join(''):'<div class="empty">没有符合条件的商品，请调整筛选或搜索。</div>';
 document.querySelectorAll('.visual img').forEach(img=>{img.addEventListener('error',()=>{img.hidden=true;img.nextElementSibling.hidden=false;});});
 document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>decide(button)));
}
async function decide(button){
 const card=button.closest('.review'),r=snapshot.items.find(x=>key(x)===card.dataset.id),action=button.dataset.action;
 if(!r||!enabled(r,action))return;
 const note=card.querySelector('textarea').value.trim()||({approve:'用户在审核台确认同款并要求上架',reject:'用户在审核台确认不同款并要求下架',list:'用户在审核台要求上架',unlist:'用户在审核台要求下架',repair:'用户要求重新识别同款'})[action];
 remember();submitting.add(key(r));clearTimeout(submissionRefreshTimer);controller?.abort();sequence++;controller=null;$('#list').setAttribute('aria-busy','false');card.querySelectorAll('button').forEach(b=>b.disabled=true);button.textContent='提交中…';card.querySelector('.action-hint').textContent='正在提交这一条，可以继续审核其他商品。';
 const reviewer=$('#reviewer').value.trim()||'匿名审核人';try{localStorage.setItem('flowhub-reviewer',reviewer);}catch{}
 const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),25000);
 try{await api('POST',{sku:r.sku,seller:r.seller,revision:r.revision,action,note,reviewer},abort.signal);drafts.delete(key(r));card.querySelector('textarea').value='';r.decision={status:'pending'};if(action!=='repair'&&view==='queue'){card.remove();snapshot.items=snapshot.items.filter(x=>key(x)!==key(r));}else{button.textContent='已提交';card.querySelector('.action-hint').textContent='已提交，可在操作记录查看进度。';}$('#notice').textContent=`SKU ${r.sku} 已提交${action==='repair'?'':'并移出待处理列表'}，可在操作记录查看进度。`;}
 catch(error){r.decision={status:'pending'};button.textContent='等待核对';const message=error.name==='AbortError'?'提交响应超时，正在刷新核对，请勿重复点击。':error.message;card.querySelector('.action-hint').textContent=message;$('#notice').textContent=`SKU ${r.sku}：${message}`;}
 finally{clearTimeout(timeout);submitting.delete(key(r));if(!submitting.size)submissionRefreshTimer=setTimeout(()=>load(),500);}
}
$('#search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=0;load();},350);});
$('#category').addEventListener('change',()=>{page=0;load();});
$('#prev').addEventListener('click',()=>{page--;load();});$('#next').addEventListener('click',()=>{page++;load();});
$('#products').addEventListener('click',()=>{view='queue';$('#category').value='';page=0;load();});$('#all').addEventListener('click',()=>{view='products';page=0;load();});$('#history').addEventListener('click',()=>{view='history';page=0;load();});
$('#refresh').addEventListener('click',()=>load());$('#retry').addEventListener('click',()=>load());
try{$('#reviewer').value=localStorage.getItem('flowhub-reviewer')||'';}catch{}
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(pollTimer);else{quietDelay=30000;load({quiet:true});}});
load();
