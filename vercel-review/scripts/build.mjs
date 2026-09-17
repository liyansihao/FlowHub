import {cp,mkdir,access} from 'node:fs/promises';
await mkdir('public',{recursive:true});
try{await access('../netlify-review/review-core.mjs');await cp('../netlify-review/public','public',{recursive:true});await cp('../netlify-review/review-core.mjs','lib/review-core.mjs');}catch(error){if(error.code!=='ENOENT')throw error;}
await access('lib/review-core.mjs');await access('public/index.html');
