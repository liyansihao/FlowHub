import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const exec=promisify(execFile);
export async function openCliCommand(session,args,timeout=25000){
 let stdout;
 try{({stdout}=await exec('opencli',['browser',session,...args],{
  timeout,maxBuffer:32*1024*1024,env:{...process.env,CI:'1'},encoding:'utf8'}));}
 catch(error){throw Error(error.killed?'navigation_timeout':'opencli_command_failed:'+String(error.code));}
 try{return JSON.parse(stdout);}catch{throw Error('opencli_response_not_json');}
}
export async function openExistingChrome({session,seller,url}){
 const opened=await openCliCommand(session,['open',url]);
 if(!opened.page)throw Error('opencli_missing_tab');
 const tab=opened.page;
 // Navigate through the already-authorized browser bridge in this dedicated Ozon
 // tab. Cookie values and storage are neither read nor exported.
 return {
  tab,
  async read(url,timeout){
   const u=new URL(url),match=u.pathname.match(/^\/seller\/(?:[^/]+-)?(\d+)\/products\/$/);
   if(u.origin!=='https://www.ozon.ru'||!match||match[1]!==seller)throw Error('identity_mismatch');
   const deadline=Date.now()+timeout;
   await openCliCommand(session,['open',url,'--tab',tab],Math.max(1,deadline-Date.now()));
   if(Date.now()>=deadline)throw Error('navigation_timeout');
   const code='({status:performance.getEntriesByType("navigation")[0]?.responseStatus||null,url:location.href,html:document.documentElement.outerHTML})';
   const response=await openCliCommand(session,['eval',code,'--tab',tab],Math.max(1,deadline-Date.now()));
   if(typeof response?.html!=='string')throw Error('browser_response_schema');
   const final=new URL(response.url);
   if(final.origin!=='https://www.ozon.ru'||!final.pathname.match(new RegExp('/seller/(?:[^/]+-)?'+seller+'/products/$')))throw Error('identity_mismatch');
   return response;
  },
  async close(){
   // Release only the session lease, never close unrelated user tabs.
   await exec('opencli',['browser',session,'close'],{timeout:10000,env:{...process.env,CI:'1'}}).catch(()=>{});
  }
 };
}
