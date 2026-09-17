// Keep the original bridge, endpoint contract and global pacing. Only its final
// ERP network hop is performed by the explicitly selected Windows device.
import {spawn} from 'node:child_process';
import path from 'node:path';
const root=path.resolve(import.meta.dirname,'..');
const originalFetch=globalThis.fetch;
globalThis.fetch=async (resource,options={})=>{
 const url=new URL(resource);
 if(url.origin!=='https://api.maozierp.com')return originalFetch(resource,options);
 const child=spawn(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/relay_erp_request.py')],{stdio:['pipe','pipe','pipe']});
 let output='';child.stdout.on('data',chunk=>{output+=chunk;});
 child.stderr.resume();
 const completion=new Promise((resolve,reject)=>{child.once('error',reject);child.once('close',resolve);});
 const timer=setTimeout(()=>child.kill(),26000);
 try{
  child.stdin.end(JSON.stringify({url:url.href,method:options.method||'GET',headers:options.headers||{},body:options.body||null}));
  await completion;
  const result=JSON.parse(output);
  if(result.error)throw new Error('remote ERP result unknown; reconcile centrally');
  return new Response(result.body,{status:result.status,headers:result.headers});
 }finally{clearTimeout(timer);}
};
await import('../../FlowEF-production/bridges/flowb.mjs');
