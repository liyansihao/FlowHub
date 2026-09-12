import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {parseArgs} from 'node:util';
import {pathToFileURL} from 'node:url';
import {chromium} from 'playwright';
import {openExistingChrome} from './storefront-opencli.mjs';

const ROOT=path.resolve(import.meta.dirname,'..');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
export const retryable=reason=>/timeout|cursor_mismatch|missing_or_wrong_product_grid|navigation|local_rpc_(timeout|transport|invalid_output)|database is locked|net::ERR_(CONNECTION|TIMED|NETWORK)/i.test(reason);
export function boundUrl(value,seller){
 const u=new URL(value),m=u.pathname.match(/^\/seller\/(?:[^/]+-)?(\d+)\/products\/$/);
 if(u.origin!=='https://www.ozon.ru'||u.hash||!m||m[1]!==seller)throw Error('identity_mismatch');
 return u.href;
}

// The engine accepts a page provider. The executable supplies Playwright; tests
// supply real saved response bodies without contacting Ozon.
export async function collect({rpc,read,save,event,seconds=600,interval=2000,maxPages=1000,
 shouldStop=()=>false,now=()=>Date.now(),delay=sleep}){
 const started=now(),deadline=started+seconds*1000;
 let committed=0,added=0,retries=0,reason='budget',lastError=null;
 while(now()<deadline&&!shouldStop()&&committed<maxPages){
  let task=rpc('status');
  if(task.state!=='ready'){reason=task.state;break;}
  let success=false;
  for(let attempt=1;attempt<=3;attempt++){
   if(now()>=deadline||shouldStop()){reason='budget';break;}
   task=rpc('status');
   if(task.state!=='ready'){reason=task.state;break;}
   const url=task.next_url;
   let artifact=null;
   try{
    const response=await read(url,Math.min(20000,deadline-now()));
    artifact=await save(task.page,attempt,response.html);
    if(response.status===403||response.status===429)throw Error('access_blocked_http_'+response.status);
    if(response.status!=null&&(response.status<200||response.status>=300))throw Error('http_'+response.status);
    const packet=rpc('validate',{html:response.html,url});
    if(!packet.rows&&!packet.explicit_end)throw Error('empty_continuing_page');
    if(now()>=deadline||shouldStop()){reason='budget';break;}
    // Re-read state after awaiting browser I/O so external pause is respected.
    if(rpc('status').state!=='ready'){reason='paused';break;}
    const result=rpc('ingest',{html:response.html,url,artifact});
    if(result.state!=='committed')throw Error(result.reason||result.state);
    committed++;added+=result.added;success=true;
    await event({type:'committed',page:task.page,rows:result.rows,added:result.added,at:now(),artifact});
    break;
   }catch(error){
    lastError=String(error.message).split('\n')[0].slice(0,220);
    if(now()>=deadline||shouldStop()){reason='budget';break;}
    // A killed IPC call can have committed just before stdout was lost.
    // Reconcile the durable audit before deciding to re-read/retry a page.
    if(/local_rpc_|database is locked/.test(lastError)){
     const recovered=rpc('recover',{url});
     if(recovered){
      committed++;added+=recovered.added;success=true;
      await event({type:'commit_recovered',page:task.page,rows:recovered.rows,added:recovered.added,at:now(),artifact});
      break;
     }
    }
    await event({type:'failed',page:task.page,attempt,reason:lastError,at:now(),artifact});
    if(!retryable(lastError)||attempt===3){
     rpc('failure',{url,reason:lastError,artifact});reason='blocked';break;
    }
    retries++;
    await delay(Math.min(1000*2**attempt,Math.max(0,deadline-now())));
   }
  }
  if(!success)break;
  if(rpc('status').state==='done'){reason='done';break;}
  await delay(Math.min(interval,Math.max(0,deadline-now())));
 }
 const end=rpc('status');
 if(end.state==='ready')rpc('pause');
 return {started_at:started/1000,ended_at:now()/1000,elapsed_seconds:(now()-started)/1000,
         committed_pages:committed,added_skus:added,retries,last_error:lastError,
         stop_reason:committed>=maxPages?'page_limit':reason,task:rpc('status'),human_interventions:0};
}

async function main(){
 const {values:o}=parseArgs({options:{
  backend:{type:'string',default:'playwright'},session:{type:'string',default:'flowhub-storefront-auto'},
  data:{type:'string'},output:{type:'string'},seller:{type:'string',default:'1168944'},
  owner:{type:'string',default:'independent-storefront'},seconds:{type:'string',default:'600'},
  interval:{type:'string',default:'2000'},profile:{type:'string'},cdp:{type:'string'},
  preflight:{type:'boolean',default:false},resume:{type:'boolean',default:false},
  python:{type:'string',default:path.join(ROOT,'.venv/bin/python')},
 }});
 if(!o.data||!o.output||!/^\d+$/.test(o.seller))throw Error('--data, --output, numeric --seller required');
 const seconds=Number(o.seconds),interval=Number(o.interval);
 if(!Number.isFinite(seconds)||seconds<1||seconds>3600||!Number.isFinite(interval)||interval<1000)throw Error('invalid budget/interval');
 const output=path.resolve(o.output);await fs.mkdir(output,{recursive:true,mode:0o700});
 const lock=await fs.open(path.join(output,'runner.lock'),'wx',0o600).catch(()=>{throw Error('runner_locked; inspect running process before removing stale lock');});
 await lock.writeFile(String(process.pid));
 let context,browser,page,existingChrome,read,stopped=false;
 process.on('SIGINT',()=>{stopped=true;});process.on('SIGTERM',()=>{stopped=true;});
 const rpc=(action,extra={})=>{
  const result=spawnSync(o.python,[path.join(ROOT,'scripts/storefront-rpc.py')],{
   input:JSON.stringify({action,data:path.resolve(o.data),owner:o.owner,seller:o.seller,...extra}),
   encoding:'utf8',maxBuffer:30*1024*1024,timeout:15000});
  if(result.error)throw Error('local_rpc_'+(result.error.code==='ETIMEDOUT'?'timeout':'transport')+':'+action+':'+result.error.code);
  let r;try{r=JSON.parse(result.stdout);}catch{throw Error('local_rpc_invalid_output:'+action+':exit_'+result.status);}
  if(!r.ok)throw Error(r.error);return r.data;
 };
 const event=async record=>{await fs.appendFile(path.join(output,'events.jsonl'),JSON.stringify(record)+'\n',{mode:0o600});console.log(JSON.stringify(record));};
 const save=async (page,attempt,html)=>{
  const file=path.join(output,'page-'+page+'-attempt-'+attempt+'-'+Date.now()+'.html');
  await fs.writeFile(file,html,{mode:0o600});return file;
 };
 try{
  // Roots are public historical provenance, never browser credentials.
  const seedsDir=path.join(ROOT,'reports/seller-expansion-hour-20260911');
  const roots=[];
  for(const stage of ['trial','supplement','supplement-pages2','supplement-pages3']){
   const file=path.join(seedsDir,stage,'seeds.json');
   try{for(const r of JSON.parse(await fs.readFile(file,'utf8')))if(String(r.seller_id)===o.seller)roots.push({...r,evidence_file:file});}
   catch(e){if(e.code!=='ENOENT')throw e;}
  }
  if(!roots.length)throw Error('source_seller_not_in_evidence');
  rpc('prepare',{roots});
  if(o.backend==='opencli'){
   existingChrome=await openExistingChrome({session:o.session,seller:o.seller,url:rpc('status').next_url});
   read=existingChrome.read;
  }else if(o.cdp){
   const endpoint=new URL(o.cdp);
   if(endpoint.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(endpoint.hostname))throw Error('local_existing_cdp_required');
   browser=await chromium.connectOverCDP(endpoint.href,{timeout:15000});
   context=browser.contexts()[0];page=await context.newPage();
  }else{
   const profile=path.resolve(o.profile||path.join(output,'browser-profile'));
   // Dedicated profile. Never attach/copy a daily browser profile or its cookies.
   if(profile.includes('Application Support/Google/Chrome'))throw Error('daily_profile_forbidden');
   context=await chromium.launchPersistentContext(profile,{channel:'chrome',headless:false,viewport:{width:1280,height:900}});
   page=await context.newPage();
  }
  if(page){
  await page.route('**/*',route=>{
   const req=route.request();
   if(req.isNavigationRequest()&&req.frame()===page.mainFrame()){
    try{boundUrl(req.url(),o.seller);}catch{return route.abort();}
   }
   return route.continue();
  });
  page.on('dialog',dialog=>dialog.dismiss().catch(()=>{}));
  read=async(url,timeout)=>{
   boundUrl(url,o.seller);
   const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:Math.max(1,timeout)});
   if(!response)throw Error('navigation_no_response');
   const html=await response.text();
   boundUrl(page.url(),o.seller);
   return {status:response.status(),html};
  };
  }
  const task=rpc('status');
  if(task.state==='done')throw Error('already_done');
  const probeStart=Date.now();
  try{
   const response=await read(task.next_url,20000);
   const artifact=await save(task.page,0,response.html);
   if(response.status!=null&&response.status!==200)throw Error('access_blocked_http_'+response.status);
   const packet=rpc('validate',{html:response.html,url:task.next_url});
   await event({type:'preflight_passed',page:packet.page,rows:packet.rows,artifact,at:Date.now()});
   await fs.writeFile(path.join(output,'preflight.json'),JSON.stringify({ok:true,page:packet.page,rows:packet.rows,started_at:probeStart/1000,ended_at:Date.now()/1000}));
  }catch(e){
   const reason=String(e.message).split('\n')[0];
   await fs.writeFile(path.join(output,'preflight.json'),JSON.stringify({ok:false,reason,at:Date.now()/1000}));
   if(page)await page.screenshot({path:path.join(output,'preflight-failure.png')}).catch(()=>{});
   await event({type:'preflight_failed',reason,at:Date.now()});
   throw Error('preflight_failed: '+reason+'; timed collection NOT started');
  }
  if(o.preflight)return;
  if(task.state==='blocked')throw Error('task_blocked; inspect audit and explicitly retry');
  if(task.state==='paused'&&!o.resume)throw Error('--resume required for paused task');
  if(o.resume)rpc('resume');
  const result=await collect({rpc,read,save,event,seconds,interval,shouldStop:()=>stopped});
  await fs.writeFile(path.join(output,'summary.json'),JSON.stringify(result,null,2));
  await fs.writeFile(path.join(output,'export.json'),JSON.stringify(rpc('export'),null,2),{mode:0o600});
  console.log(JSON.stringify({type:'finished',...result}));
 }finally{
  if(existingChrome)await existingChrome.close();
  if(page)await page.close().catch(()=>{});
  if(browser)await browser.close().catch(()=>{});
  else if(context)await context.close().catch(()=>{});
  await lock.close();await fs.unlink(path.join(output,'runner.lock')).catch(()=>{});
 }
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
 main().catch(error=>{console.error(String(error.message));process.exitCode=1;});
}
