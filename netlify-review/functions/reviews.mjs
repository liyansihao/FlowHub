import {createReviewHandler} from '../review-core.mjs';
import fs from 'node:fs/promises';
import { getStore } from '@netlify/blobs';

export async function readSeedFile(moduleUrl=import.meta.url) {
  // Source tree: functions/reviews.mjs; esbuild archive: reviews.mjs at root.
  for (const relative of ['../data/reviews.json','./data/reviews.json']) {
    try {return JSON.parse(await fs.readFile(new URL(relative,moduleUrl),'utf8'));}
    catch(error) {if (error.code!=='ENOENT') throw error;}
  }
  throw new Error('审核兜底文件未打包，请检查 functions.included_files');
}
export function createHandler({store,readSeed=readSeedFile}={}) {
  return request => createReviewHandler({store:store || getStore({name:'flowhub-review',consistency:'strong'}),readSeed,syncToken:()=>process.env.REVIEW_SYNC_TOKEN})(request);
}
export default createHandler();
