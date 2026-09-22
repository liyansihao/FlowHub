const $=s=>document.querySelector(s),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let data={items:[],stores:[]},kind='returns',page=0,busy=false;
const time=x=>x?new Date(typeof x==='number'?x*1000:x).toLocaleString('zh-CN',{hour12:false}):'等待同步';
async function api(mode='view',body){const r=await fetch('/api/operations?mode='+mode,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Ops-Request':'1'},signal:AbortSignal.timeout(30000),...(body?{body:JSON.stringify(body)}:{})});const d=await r.json();if(!r.ok){if(r.status===401){$('#login').hidden=false;$('#workspace').hidden=true;}throw Error(d.error||'请求失败');}return d;}
function error(e){$('#error').textContent=e.message;$('#error').hidden=false;}
function render(){
 const store=$('#store').value,handled=$('#handled').value,q=$('#search').value.trim().toLowerCase();
 for(const k of ['returns','inventory'])$('#'+k+'-count').textContent=data.items.filter(r=>r.kind===k&&!r.handling.handled&&(!store||r.store_id===store)).length+' 待处理';
 const rows=[...data.items].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||''))).filter(r=>r.kind===kind&&(!store||r.store_id===store)&&(handled==='all'||r.handling.handled===(handled==='done'))&&(!q||[r.name,r.offer_id,r.order,r.sku,r.store_name].join(' ').toLowerCase().includes(q)));
 page=Math.min(page,Math.max(0,Math.ceil(rows.length/50)-1));
 $('#count').textContent=rows.length+' 条记录';$('#page').textContent=`${page+1} / ${Math.max(1,Math.ceil(rows.length/50))}`;$('#prev').disabled=page===0;$('#next').disabled=(page+1)*50>=rows.length;
 $('#rows').innerHTML=rows.slice(page*50,page*50+50).map(r=>{
 const sku=/^[1-9][0-9]{0,19}$/.test(r.sku)?r.sku:null,stale=r.stale||Date.now()/1000-r.synced>(r.kind==='returns'?1200:1800);
 return `<tr><td>${sku?`<a href="https://www.ozon.ru/product/${sku}/" target="_blank" rel="noreferrer">${esc(r.name||r.offer_id)} ↗</a>`:esc(r.name||r.offer_id)}<div class="sub">${esc(r.order||r.offer_id||r.id)}</div>${r.reason?`<div class="sub">${esc(typeof r.reason==='string'?r.reason:JSON.stringify(r.reason))}</div>`:''}</td><td>${esc(r.store_name)}</td><td>${kind==='inventory'?'<span class="warning">库存 0</span>':esc(r.state_label||r.state)}${kind==='returns'?`<div class="sub">申请 ${time(r.created_at)}</div>`:''}</td><td>${time(r.synced)}${stale?'<div class="sub warning">数据待更新</div>':''}</td><td>${r.handling.handled?`<div class="done">✓ 已经处理</div><div class="sub">${time(r.handling.at)}</div>`:''}<button class="${r.handling.handled?'undo':'primary'}" data-key="${esc(r.key)}" ${busy?'disabled':''}>${r.handling.handled?'撤销处理':'已经处理'}</button></td></tr>`;
 }).join('')||'<tr><td colspan="5" class="empty">当前筛选下没有记录</td></tr>';
}
async function load(){if(busy)return;data=await api();$('#login').hidden=true;$('#workspace').hidden=false;$('#error').hidden=true;const selected=$('#store').value;$('#store').innerHTML='<option value="">全部店铺</option>'+data.stores.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');$('#store').value=selected;
 const missing=data.stores.filter(s=>!s.channels?.[kind]?.success).length;
 $('#sync').textContent=`${data.stores.length} 家店铺 · 云端更新 ${time(data.uploaded_at)}${Date.now()-Date.parse(data.uploaded_at)>720000?' · 本机同步延迟，当前为上次数据':''}${missing?' · '+missing+' 家当前通道尚未成功同步':''}`;render();}
document.querySelectorAll('[data-kind]').forEach(b=>b.onclick=()=>{kind=b.dataset.kind;page=0;document.querySelectorAll('[data-kind]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));render();});
for(const id of ['store','handled','search'])$('#'+id).addEventListener(id==='search'?'input':'change',()=>{page=0;render();});
$('#prev').onclick=()=>{page--;render();};$('#next').onclick=()=>{page++;render();};$('#refresh').onclick=()=>load().catch(error);
$('#rows').onclick=async e=>{const b=e.target.closest('[data-key]');if(!b||busy)return;const row=data.items.find(r=>r.key===b.dataset.key);busy=true;render();let failure;try{await api('handle',{key:row.key,handled:!row.handling.handled,version:row.handling.version});$('#notice').textContent=row.handling.handled?'已撤销处理标记':'已记录处理完成，可在「已经处理」中查看或撤销';}catch(e){failure=e;}finally{busy=false;await load().catch(error);if(failure)error(failure);}};
$('#login-form').onsubmit=async e=>{e.preventDefault();try{await api('session',{token:$('#access-token').value.trim()});$('#access-token').value='';await load();}catch(e){error(e);}};
const fragment=new URLSearchParams(location.hash.slice(1)),token=fragment.get('access');if(token)history.replaceState(null,'',location.pathname);
try{if(token)await api('session',{token});await load();}catch(e){error(e);}
setInterval(()=>{if(!document.hidden&&!$('#workspace').hidden)load().catch(error);},60000);
