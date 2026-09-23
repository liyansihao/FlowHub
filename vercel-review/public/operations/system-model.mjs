export const labels={running:'运行中',paused_new:'已暂停新增',stopped:'已停止',unresponsive:'心跳异常',unknown:'状态待确认',needs_fields:'待补资料',needs_review:'待审核',publishing:'发布处理中',awaiting_remote:'等待平台结果',delisting:'下架处理中',selling:'在售',quarantined:'已隔离',rejected:'已拒绝',not_listed:'未上架',delisted:'已下架',failed:'失败'};
export const stateLabel=value=>labels[value]??value??'未知';
export const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const number=value=>value===null||value===undefined||value===''||!Number.isFinite(Number(value))?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:1});
export const time=(value,options={})=>!value||!Number.isFinite(Number(value))?'暂无时间':new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23',...options}).format(new Date(Number(value)*1000));
export const hour=value=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value*1000)).find(p=>p.type==='hour').value;
export const isStale=(data,now=Date.now()/1000)=>!data||data.stale||!Number.isFinite(data.observed_at)||now-data.observed_at>90;
export function runningState(data,now){return isStale(data,now)?'unknown':data.status?.state??'unknown';}
export function identities(items){
 const groups=new Map();
 for(const item of items??[]){const b=item.body??{},key=JSON.stringify([b.owner,b.sku,b.seller]);if(!groups.has(key))groups.set(key,{owner:b.owner,sku:b.sku,seller:b.seller});groups.get(key)[item.kind]=b;}
 return [...groups.values()];
}
export const errorLabel=value=>({sqlite_lock:'SQLite 锁竞争',storage:'存储异常',external_or_transport:'外部接口 / 网络异常',application:'程序异常'}[value]??value??'未分类异常');

export const verifiedFlag=value=>value===true||value===1;
