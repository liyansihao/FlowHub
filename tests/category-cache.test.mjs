import test from 'node:test';
import assert from 'node:assert/strict';
import {cachedCategory} from '../bridges/category-cache.mjs';
const data={sku:'1',cate:[1,2,3],product_info:{weight:40}};
const product={sku:'1',direct_source_facts:{sku:'1',source:'maozi-category-by-sku',observed_at:100,category_response:data}};
test('category reuse requires fresh matching SKU and known category',()=>{
 assert.equal(cachedCategory(product,101),data);
 for(const now of [99,21700])assert.equal(cachedCategory(product,now),null);
 assert.equal(cachedCategory({...product,sku:'2'},101),null);
 for(const patch of [{source:'other'},{category_response:{...data,sku:'2'}},{category_response:{...data,cate:[]}}])
  assert.equal(cachedCategory({...product,direct_source_facts:{...product.direct_source_facts,...patch}},101),null);
});
