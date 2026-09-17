import {DurableObject} from 'cloudflare:workers';
import {createReviewHandler} from '../../netlify-review/review-core.mjs';
import {ReviewStore} from './store.mjs';
const json=(body,status=200)=>Response.json(body,{status,headers:{'cache-control':'no-store'}});
export class ReviewQueue extends DurableObject {
 constructor(ctx,env){super(ctx,env);this.store=new ReviewStore(ctx.storage);this.handler=createReviewHandler({store:this.store,syncToken:()=>env.REVIEW_SYNC_TOKEN});}
 async fetch(request){return this.ctx.blockConcurrencyWhile(async()=>{
  const mode=new URL(request.url).searchParams.get('mode');
  if(mode==='import-history'){
   if(request.method!=='POST')return json({error:'method_not_allowed'},405);
   if(!this.env.REVIEW_SYNC_TOKEN||request.headers.get('x-sync-token')!==this.env.REVIEW_SYNC_TOKEN)return json({error:'unauthorized'},401);
   const body=await request.json(),snapshot=await this.store.get('snapshot');
   if(!snapshot||body.owner!==snapshot.owner)return json({error:'owner_mismatch'},409);
   if(!Array.isArray(body.items)||body.items.length>1000||body.items.some(x=>!x||typeof x.id!=='string'||x.id.length>100||!['pending','applied','rejected'].includes(x.status)||!['approve','reject','repair','list','unlist'].includes(x.action)))return json({error:'invalid_history'},422);
   // Idempotent import; never overwrite a command that has since been acknowledged.
   let imported=0;
   this.store.transaction(()=>{for(const item of body.items){const key=`decisions/${encodeURIComponent(item.id)}`;if(!this.store.read(key)){this.store.write(key,JSON.stringify({...item,owner:body.owner}));imported++;}}});
   return json({ok:true,imported});
  }
  return this.handler(request);
 });}
}
export default {
 async fetch(request,env){const path=new URL(request.url).pathname;
  // Keep the old database for later reconciliation; stop all old-site writes.
  const recoveryRead=request.method==='GET'&&env.REVIEW_SYNC_TOKEN&&request.headers.get('x-sync-token')===env.REVIEW_SYNC_TOKEN;
  if(path==='/api/reviews'&&!recoveryRead)return json({error:'审核台已迁移，请打开新网址',url:'https://flowhub-review.vercel.app'},410);
  if(!path.startsWith('/api/'))return Response.redirect('https://flowhub-review.vercel.app/',302);
  if(path==='/api/reviews'){
   try{return await env.REVIEW_QUEUE.get(env.REVIEW_QUEUE.idFromName('main')).fetch(request);}
   catch(error){const quota=/Exceeded allowed rows|free tier/i.test(String(error));return json({error:quota?'审核服务今日数据库免费额度已用尽，额度将在北京时间次日08:00重置，或升级套餐后恢复。商品数据及已提交记录仍保留。':'审核服务暂时不可用，请稍后重试。',code:quota?'storage_quota_exceeded':'review_unavailable'},503);}
  }
  if(path.startsWith('/api/'))return json({error:'not_found'},404);
  return env.ASSETS.fetch(request);
 }
};
