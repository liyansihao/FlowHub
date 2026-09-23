import {stateLabel,escapeHTML as esc,number as num,time,hour,isStale,runningState,identities,errorLabel} from './system-model.mjs';
const $=id=>document.getElementById(id);
const cache={},failures=new Set();let refreshing=false,taskGeneration=0,taskController,taskLoading=false,cursors=[''],nextCursor=null,productGeneration=0,productController;
const empty=text=>`<div class="sys-empty">${esc(text)}</div>`;
const skuLink=(sku,seller='')=>sku?`<button class="sys-link" data-sys-sku="${esc(sku)}" data-sys-seller="${esc(seller)}">${esc(sku)}</button>`:'<span>系统任务</span>';
async function api(path,signal){
 const response=await fetch(`/api/system/v1/${path}`,{cache:'no-store',signal:signal??AbortSignal.timeout(20000)});
 if(!response.ok)throw new Error(`HTTP ${response.status}`);
 return response.json();
}
function panelNotice(name,data){
 const node=$(`sys-${name}-error`),issues=[];
 if(failures.has(name))issues.push('本次连接失败；已保留上次数据，正在等待刷新。');
 if(data&&isStale(data))issues.push('本机数据已过期，请勿按实时状态判断。');
 if(!data&&!failures.has(name))return;
 node.textContent=issues.join(' ');node.hidden=!issues.length;
}
function renderStatus(){
 const d=cache.status;if(!d){if(failures.has('status')){$('sys-status').innerHTML='<i class="sys-dot"></i><span>状态待确认</span>';$('sys-freshness').textContent='暂时无法读取运行状态';}panelNotice('status');return;}
 const s=d.status??{},state=failures.has('status')?'unknown':runningState(d);
 $('sys-status').innerHTML=`<i class="sys-dot ${state==='running'?'live':''}"></i><span>${esc(stateLabel(state))}</span><span class="sys-tag">${esc(d.source_tag??'版本未知')}</span>`;
 $('sys-freshness').textContent=`核心心跳 ${time(s.heartbeat_at)} · 数据更新 ${time(d.observed_at)}${state==='unknown'?' · 上次记录：'+stateLabel(s.last_known_state??s.state):''}`;
 $('sys-pending').textContent=num(s.pending_task_count);
 panelNotice('status',d);
 if(s.collection_errors?.length){$('sys-status-error').hidden=false;$('sys-status-error').textContent+=' 部分运行数据采集异常，状态可能不完整。';}
 $('sys-task-counts').textContent=`隔离 ${num(s.queue_counts?.quarantined)} 件（不含在待处理总数中）。商品列表分批同步，可能晚于运行总览。`;
}
function renderMetrics(){
 const d=cache.metrics;if(!d){panelNotice('metrics');return;}
 $('sys-success').textContent=num(d.successes);$('sys-speed').textContent=num(d.last_60_minutes);
 $('sys-failures').textContent=d.failures==null?'待核实':num(d.failures);
 $('sys-failure-note').textContent=d.failures==null?'失败时间记录不完整，不按 0 计算':'最终失败商品数';
 $('sys-metrics-date').textContent=`${d.date??'今日'} · 上海时间`;
 const hours=d.hours??[],max=Math.max(1,...hours.map(h=>h.successes));
 $('sys-chart').innerHTML=hours.map(h=>{
  const future=h.seconds_observed<=0,partial=h.seconds_observed>0&&h.seconds_observed<3600;
  const label=`${hour(h.start)}:00，${future?'尚未开始':`${num(h.successes)} 件${partial?'，当前小时未结束':''}`}`;
  return `<div class="sys-bar-cell" tabindex="0" aria-label="${esc(label)}"><div class="sys-bar-track"><div class="sys-bar ${future?'future':partial?'partial':''}" style="height:${future?0:Math.max(1,Number(h.successes)/max*100)}%"></div></div><span>${esc(hour(h.start))}</span></div>`;
 }).join('');
 const c=d.cohort??{},rate=c.success_rate_including_pending;
 $('sys-cohort').textContent=`今日发起 ${num(c.enrolled)} 件：已核验 ${num(c.verified)}，尚未核验 ${num(c.not_verified)}。成功率 ${rate==null?'暂无分母':num(rate*100)+'%'}（未完成保留在分母中）。`;
 const coverage=d.coverage?.coverage??{};
 $('sys-metrics-note').textContent=`只计首次核验成功；浅色为当前未满一小时。${d.complete?'':'数据尚未完整同步。'}统计来自分批同步记录，发布数据检查点 ${time(coverage.plugin_publications)}。`;
 $('sys-steps').innerHTML=(d.steps??[]).map(s=>`<div class="sys-row"><div><strong>${esc(s.module)}</strong><div class="sys-caption">${num(s.attempts)} 次执行 · ${num(s.errors)} 次异常</div></div><div class="sys-caption">平均 ${num(s.mean_seconds)} 秒<br>最长 ${num(s.max_seconds)} 秒</div></div>`).join('')||empty('今日暂无步骤耗时记录。');
 panelNotice('metrics',d);
}
function taskRows(items){return items.map(raw=>{
 const b=raw.body??raw;
 return `<div class="sys-row"><div>${skuLink(b.sku,b.seller)}<div class="sys-caption">店铺 ${esc(b.seller||'未分配')} · ${esc(b.owner||'来源未记录')}<br>尝试 ${num(b.attempts)} 次${b.observed_at?' · 更新 '+esc(time(b.observed_at)):''}</div></div><span class="sys-tag">${esc(stateLabel(b.state))}</span></div>`;
 }).join('');}
function renderActivity(){const d=cache.activity;if(d)$('sys-activity').innerHTML=taskRows(d.items??[])||empty('当前没有已领取的任务。可结合待处理队列判断是否有等待中的商品。');panelNotice('activity',d);}
function taskButtons(){ $('sys-task-prev').disabled=taskLoading||cursors.length<=1;$('sys-task-next').disabled=taskLoading||!nextCursor;$('sys-task-page').textContent=`第 ${cursors.length} 页`; }
async function loadTasks(){
 const generation=++taskGeneration;taskController?.abort();taskController=new AbortController();taskLoading=true;taskButtons();
 const controller=taskController,timer=setTimeout(()=>controller.abort(),20000);
 try{
  const params=new URLSearchParams({limit:'8',cursor:cursors.at(-1)}),state=$('sys-task-state').value;if(state)params.set('state',state);
  const d=await api(`tasks?${params}`,controller.signal);if(generation!==taskGeneration)return;
  cache.tasks=d;failures.delete('tasks');nextCursor=d.next_cursor;
  $('sys-tasks').innerHTML=taskRows(d.items??[])||empty('这个状态下暂无商品记录。');panelNotice('tasks',d);
 }catch(e){if(generation!==taskGeneration)return;failures.add('tasks');nextCursor=null;panelNotice('tasks',cache.tasks);}
 finally{clearTimeout(timer);if(generation===taskGeneration){taskLoading=false;taskButtons();}}
}
function eventRows(events){return events.map(e=>`<div class="sys-row"><div><div class="${e.error_class?'sys-error-head':''}">${esc(e.error_class?errorLabel(e.error_class):e.outcome??'已记录')}</div><div class="sys-caption">${esc(e.module??'未标记步骤')} · ${esc(time(e.finished||e.started))}${e.duration_seconds!=null?' · '+num(e.duration_seconds)+' 秒':''}</div>${e.sku?skuLink(e.sku):''}</div></div>`).join('');}
function renderErrors(){
 const d=cache.errors;if(!d){panelNotice('errors');return;}
 $('sys-errors').innerHTML=eventRows(d.items??[])||empty('当前同步的步骤记录中没有异常。');
 if(d.logs?.length)$('sys-errors').innerHTML+=`<details class="sys-details"><summary>运行日志异常摘要（${d.logs.length} 条）</summary>${d.logs.map(l=>`<div class="sys-row"><div>${esc(errorLabel(l.class))}<div class="sys-caption">${esc(l.source)} · ${l.at?esc(time(l.at)):'原始时间未记录'}${l.historical_tail?' · 历史日志尾部':''}</div></div></div>`).join('')}</details>`;
 panelNotice('errors',d);
}
const renders={status:renderStatus,metrics:renderMetrics,activity:renderActivity,errors:renderErrors};
async function refresh(){
 if(refreshing)return;refreshing=true;$('sys-refresh').disabled=true;$('sys-refresh').textContent='刷新中…';
 try{await Promise.allSettled([...Object.keys(renders).map(async name=>{
  try{cache[name]=await api(name==='errors'?'errors?limit=12':name);failures.delete(name);}catch{failures.add(name);}
  if(failures.has(name)&&!cache[name]){const target={metrics:'sys-cohort',activity:'sys-activity',errors:'sys-errors'}[name];if(target)$(target).textContent='暂时无法获取数据，请稍后刷新。';}renders[name]();
 }),loadTasks()]);}finally{refreshing=false;$('sys-refresh').disabled=false;$('sys-refresh').textContent='刷新运行数据';}
}
function productHTML(d){
 const groups=identities(d.items);
 const notice=isStale(d)?'<p class="sys-alert">本机数据已过期，以下为最后一次同步记录。</p>':'';
 return notice+(groups.map(g=>{
  const product=g.product??{},pub=g.publication??{},success=g.success??{};
  const verified=pub.verified===true||success.verified===true;
  return `<section class="sys-identity"><h3>店铺 ${esc(g.seller||'未分配')}</h3><dl><dt>来源</dt><dd>${esc(g.owner||'未记录')}</dd><dt>当前记录状态</dt><dd>${esc(stateLabel(product.state))}</dd><dt>发布核验</dt><dd>${verified?'已核验成功':'尚未核验成功'}</dd><dt>尝试次数</dt><dd>${num(product.attempts)}</dd><dt>下一次计划</dt><dd>${product.due?esc(time(product.due)):'未记录'}</dd><dt>发布开始</dt><dd>${esc(time(pub.started_at))}</dd><dt>首次核验</dt><dd>${esc(time(success.first_verified_at??pub.first_verified_at))}</dd><dt>发布方式</dt><dd>${esc(pub.backend??success.backend??'未记录')}</dd><dt>商品货号</dt><dd>${esc(pub.offer_id??success.offer_id??'未记录')}</dd><dt>状态同步时间</dt><dd>${esc(time(product.observed_at??pub.observed_at??success.observed_at))}</dd></dl></section>`;
 }).join('')||empty('尚未同步到该商品记录，请检查来源 SKU 和店铺 ID。'))+`<h3>最近处理步骤</h3><p>同一 SKU 的步骤可能来自不同店铺；尚未提供逐店铺归属。历史成功核验不代表当前仍在售。</p><div class="sys-list">${eventRows(d.events??[])||empty('暂无已同步步骤记录。')}</div>`;
}
async function openProduct(sku,seller=''){
 const generation=++productGeneration;productController?.abort();productController=new AbortController();const controller=productController;
 const dialog=$('sys-product-dialog');$('sys-product-title').textContent=`商品 ${sku}`;$('sys-product-body').innerHTML=empty('正在读取商品详情…');if(!dialog.open)dialog.showModal();
 const timer=setTimeout(()=>controller.abort(),20000);
 try{const params=new URLSearchParams();if(seller)params.set('seller_id',seller);const d=await api(`products/${encodeURIComponent(sku)}?${params}`,controller.signal);if(generation===productGeneration)$('sys-product-body').innerHTML=productHTML(d);}
 catch{if(generation===productGeneration)$('sys-product-body').innerHTML='<p class="sys-alert">商品详情暂时无法读取，请关闭后重新查询。</p>';}
 finally{clearTimeout(timer);}
}
$('sys-refresh').addEventListener('click',refresh);
$('sys-task-state').addEventListener('change',()=>{cursors=[''];nextCursor=null;cache.tasks=null;$('sys-tasks').innerHTML=empty('正在切换任务状态…');loadTasks();});
$('sys-task-prev').addEventListener('click',()=>{if(!taskLoading&&cursors.length>1){cursors.pop();cache.tasks=null;$('sys-tasks').innerHTML=empty('正在读取上一页…');loadTasks();}});
$('sys-task-next').addEventListener('click',()=>{if(!taskLoading&&nextCursor){cursors.push(nextCursor);cache.tasks=null;$('sys-tasks').innerHTML=empty('正在读取下一页…');loadTasks();}});
$('sys-product-form').addEventListener('submit',e=>{e.preventDefault();const sku=$('sys-product-sku').value.trim();if(sku)openProduct(sku,$('sys-product-seller').value.trim());});
for(const root of [$('system-dashboard'),$('sys-product-dialog')])root.addEventListener('click',e=>{const button=e.target.closest('[data-sys-sku]');if(button)openProduct(button.dataset.sysSku,button.dataset.sysSeller);});
$('sys-product-close').addEventListener('click',()=>$('sys-product-dialog').close());
$('sys-product-dialog').addEventListener('close',()=>{++productGeneration;productController?.abort();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{renderStatus();for(const name of ['metrics','activity','tasks','errors'])panelNotice(name,cache[name]);if(!document.hidden)refresh();},60000);
refresh();
