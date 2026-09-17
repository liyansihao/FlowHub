const response = (body, status=200) => new Response(JSON.stringify(body), {
  status, headers: {'content-type':'application/json; charset=utf-8','cache-control':'no-store'},
});
function token(request, expected) {
  if (!expected || request.headers.get('x-sync-token')!==expected) throw new Error('同步凭据无效或尚未配置');
}
const sameItem=(a,b)=>a.sku===b.sku && a.seller===b.seller && a.revision===b.revision;
const allowed=(item,action)=>Array.isArray(item.allowed_actions) && item.allowed_actions.includes(action)
  && (action!=='approve' || item.can_approve===true) && (action!=='repair' || item.can_repair===true) && (action!=='list' || item.can_list===true) && (action!=='unlist' || item.can_unlist===true);

// Separate blobs prevent refresh/ack/reviewer writes from overwriting each other.
// The local revision-bound transaction arbitrates concurrent reviewers.
export function createReviewHandler({store, readSeed=async()=>({items:[]}), syncToken}={}) {
  return async request => {
    try {
      const mode=new URL(request.url).searchParams.get('mode') || 'review';
      if (!['sync','ack','refresh','review'].includes(mode)) return response({error:'未知操作'},400);
      const methods={sync:['GET'],ack:['POST'],refresh:['POST'],review:['GET','POST']};
      if (!methods[mode].includes(request.method)) return response({error:'method_not_allowed'},405);
      if (mode!=='review') { try {token(request,syncToken());} catch(error){return response({error:error.message},401);} }
      let input={};
      if (request.method==='POST') {
        try {input=await request.json();} catch {return response({error:'请求正文必须是 JSON'},422);}
        if (!input || typeof input!=='object' || Array.isArray(input)) return response({error:'请求正文无效'},422);
      }
      const legacy=await store.get('state',{type:'json'}) || {};
      const current=['sync','ack'].includes(mode)?null:await store.get('snapshot',{type:'json'}) || legacy.snapshot;
      async function decisions() {
        const list=await store.list({prefix:'decisions/'});
        const entries=await Promise.all(list.blobs.map(x=>store.get(x.key,{type:'json'})));
        const byId=new Map((legacy.decisions || []).map(x=>[x.id,x]));
        for(const item of entries.filter(Boolean)) byId.set(item.id,item);
        return [...byId.values()];
      }
      if (mode==='refresh') {
        if (!Array.isArray(input.items) || !input.owner || input.items.length>10000 || input.items.some(x=>!x || typeof x.sku!=='string' || typeof x.seller!=='string' || !/^[a-f0-9]{64}$/.test(x.revision)))
          return response({error:'invalid_snapshot'},422);
        if (current?.owner && current.owner!==input.owner) return response({error:'审核台已绑定其他租户'},409);
        const next={generated_at:input.generated_at || new Date().toISOString(),owner:input.owner,total:input.items.length,items:input.items,local_history:Array.isArray(input.local_history)?input.local_history.slice(0,1000):[]};
        await store.setJSON('snapshot',next);
        return response({ok:true,total:next.total,generated_at:next.generated_at});
      }
      const history=await decisions();
      if (mode==='sync') return response({items:history.filter(x=>x.status==='pending')});
      if (mode==='ack') {
        if (!['applied','rejected','error'].includes(input.status)) return response({error:'invalid_status'},422);
        const item=history.find(x=>x.id===input.id);
        if (!item) return response({error:'decision_not_found'},404);
        // Terminal acknowledgements cannot be undone by a delayed retry.
        if (['applied','rejected'].includes(item.status)) return response({ok:true,status:item.status});
        item.status=input.status==='error'?'pending':input.status;
        item.last_error=String(input.error || '').slice(0,500);item.acked_at=new Date().toISOString();
        await store.setJSON(`decisions/${encodeURIComponent(item.id)}`,item);
        return response({ok:true,status:item.status});
      }
      if (request.method==='GET') {
        const snapshot=current || await readSeed();
        const url=new URL(request.url), paged=url.searchParams.has('page');
        const merged=[...history,...(snapshot.local_history || []).filter(local=>!history.some(remote=>remote.owner===snapshot.owner && sameItem(remote,local) && remote.action===local.action))].sort((a,b)=>(b.created_at || '').localeCompare(a.created_at || ''));
        const q=(url.searchParams.get('q') || '').trim().toLowerCase();
        const state=url.searchParams.get('state') || '';
        const historyView=url.searchParams.get('view')==='history';
        const queueView=url.searchParams.get('view')==='queue';
        const handled=new Set(history.filter(x=>x.owner===snapshot.owner && ['approve','reject','list','unlist'].includes(x.action) && ['pending','applied','rejected'].includes(x.status)).map(x=>`${x.sku}:${x.seller}`));
        const reviewQueue=snapshot.items.filter(item=>item.pipeline_state==='needs_review' && !handled.has(`${item.sku}:${item.seller}`));
        const itemMap=new Map(snapshot.items.map(x=>[`${x.sku}:${x.seller}`,x]));
        const newest=new Map();
        for(const event of merged)if(!newest.has(`${event.sku}:${event.seller}`))newest.set(`${event.sku}:${event.seller}`,event.id);
        const visibleHistory=merged.map(event=>{const item=itemMap.get(`${event.sku}:${event.seller}`);return newest.get(`${event.sku}:${event.seller}`)===event.id?{...event,listing_state:item?.listing_state,listing_error:item?.listing_error,target_store_name:item?.target_store_name}:event;});
        const matches=item=>!q || [item.sku,item.title,item.supplier_title,item.target_store_name,item.note].some(v=>String(v || '').toLowerCase().includes(q));
        let selected=historyView?visibleHistory.filter(matches):(queueView?reviewQueue:snapshot.items).filter(item=>matches(item) && (!state || (state==='pending'?item.pipeline_state==='needs_review':state==='selling'?item.pipeline_state==='selling':state==='delisted'?['delisted','not_listed'].includes(item.pipeline_state):state==='blocked'?item.listing_state==='blocked':state==='processing'?['queued','running','waiting'].includes(item.listing_state):false)));
        const filteredTotal=selected.length;
        const size=Math.max(1,Math.min(48,Number.parseInt(url.searchParams.get('page_size'),10)||12));
        const pages=Math.max(1,Math.ceil(filteredTotal/size));
        const page=Math.max(0,Math.min(pages-1,Number.parseInt(url.searchParams.get('page'),10)||0));
        if(paged) selected=selected.slice(page*size,(page+1)*size);
        const latest=new Map();
        for(const entry of history.filter(x=>x.owner===snapshot.owner).sort((a,b)=>(a.created_at || '').localeCompare(b.created_at || ''))) latest.set(`${entry.sku}:${entry.seller}:${entry.revision}`,entry);
        return response({generated_at:snapshot.generated_at,total:snapshot.items.length,queue_total:reviewQueue.length,filtered_total:filteredTotal,page,page_size:size,pages,
          items:historyView?[]:selected.map(item=>({...item,
            ...(current?{}:{can_approve:false,can_repair:false,can_reject:false,can_list:false,can_unlist:false,allowed_actions:[],approval_block:'等待本机同步最新结果'}),
            decision:latest.get(`${item.sku}:${item.seller}:${item.revision}`) || null,
          })),history:historyView?selected:paged?[]:merged});
      }
      if (!current) return response({error:'等待本机同步最新审核快照，请稍后刷新'},409);
      const item=current.items.find(x=>x.sku===input.sku && x.seller===input.seller);
      if (!item) return response({error:'商品已不在当前待审队列，请刷新'},404);
      if (!['approve','reject','repair','list','unlist'].includes(input.action)) return response({error:'invalid_action'},422);
      if (input.revision!==item.revision) return response({error:'审核版本已变化，请刷新页面'},409);
      if (!allowed(item,input.action)) return response({error:(input.action==='repair'?item.repair_block:item.approval_block) || '当前商品不允许此操作'},409);
      if (typeof input.note!=='string' || !input.note.trim() || input.note.length>1000) return response({error:'请填写1至1000字的审核理由'},422);
      if (history.some(x=>x.owner===current.owner && sameItem(x,item) && ['pending','applied'].includes(x.status))) return response({error:'该版本已有审核决定，请等待同步或刷新'},409);
      const decision={id:crypto.randomUUID(),owner:current.owner,sku:item.sku,seller:item.seller,action:input.action,
        note:input.note.trim(),reviewer:String(input.reviewer || '匿名审核人').trim().slice(0,80) || '匿名审核人',
        revision:item.revision,status:'pending',created_at:new Date().toISOString()};
      await store.setJSON(`decisions/${decision.id}`,decision);
      return response({ok:true,status:'pending',decision});
    } catch(error) { return response({error:error.message || '请求未完成'},500); }
  };
}
