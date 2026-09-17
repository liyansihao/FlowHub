export const queueWhere=`p.state='needs_review' AND NOT EXISTS (
 SELECT 1 FROM flowhub_review_decisions d WHERE d.owner=p.owner AND d.sku=p.sku AND d.seller=p.seller
 AND d.action IN ('approve','reject','list','unlist') AND d.status IN ('pending','applied','rejected'))`;
const historyWhere=`NOT (left(d.id,6)='local-' AND EXISTS (SELECT 1 FROM flowhub_review_decisions other
 WHERE left(other.id,6)!='local-' AND other.owner=d.owner AND other.sku=d.sku AND other.seller=d.seller
 AND other.revision=d.revision AND other.action=d.action))`;
export class Repository{
 constructor(client){this.c=client;}
 async meta(){return (await this.c.query('SELECT *,viewer_at>now()-interval \'3 minutes\' AS viewer_active FROM flowhub_review_meta WHERE id=1')).rows[0]??null;}
 async touch(){await this.c.query("UPDATE flowhub_review_meta SET viewer_at=now() WHERE id=1 AND viewer_at<now()-interval '60 seconds'");}
 async bump(){await this.c.query('UPDATE flowhub_review_meta SET version=version+1 WHERE id=1');}
 async product(owner,sku,seller){return (await this.c.query('SELECT data FROM flowhub_review_products WHERE owner=$1 AND sku=$2 AND seller=$3',[owner,sku,seller])).rows[0]?.data;}
 async stats(owner){return (await this.c.query(`SELECT count(*)::int AS total,count(*) FILTER(WHERE ${queueWhere})::int AS queue_total FROM flowhub_review_products p WHERE p.owner=$1`,[owner])).rows[0];}
 async products(owner,{view,q,state,size,requestedPage}){
  let where='p.owner=$1 AND ($2=\'\' OR position(lower($2) in p.search_text)>0)';
  if(view==='queue')where+=` AND ${queueWhere}`;
  const filters={pending:"p.state='needs_review'",selling:"p.state='selling'",delisted:"p.state IN ('delisted','not_listed')",blocked:"p.listing_state='blocked'",processing:"p.listing_state IN ('queued','running','waiting')"};
  if(state)where+=' AND '+(filters[state]||'false');
  const total=(await this.c.query(`SELECT count(*)::int AS n FROM flowhub_review_products p WHERE ${where}`,[owner,q])).rows[0].n;
  const pages=Math.max(1,Math.ceil(total/size)),page=Math.min(requestedPage,pages-1);
  const rows=(await this.c.query(`SELECT p.data,decision.data AS decision FROM flowhub_review_products p
   LEFT JOIN LATERAL (SELECT d.data FROM flowhub_review_decisions d WHERE d.owner=p.owner AND d.sku=p.sku AND d.seller=p.seller AND d.revision=p.revision ORDER BY d.created_at DESC,d.id DESC LIMIT 1) decision ON true
   WHERE ${where} ORDER BY p.sku COLLATE "C",p.seller COLLATE "C" LIMIT $3 OFFSET $4`,[owner,q,size,page*size])).rows;
  return {filtered_total:total,pages,page,items:rows.map(r=>({...r.data,decision:r.decision??null})),history:[]};
 }
 async history(owner,{q,size,requestedPage}){
  const where=`d.owner=$1 AND ${historyWhere} AND ($2='' OR position(lower($2) in lower(concat_ws(' ',d.sku,d.data->>'note',p.search_text)))>0)`;
  const join='LEFT JOIN flowhub_review_products p ON p.owner=d.owner AND p.sku=d.sku AND p.seller=d.seller';
  const total=(await this.c.query(`SELECT count(*)::int AS n FROM flowhub_review_decisions d ${join} WHERE ${where}`,[owner,q])).rows[0].n;
  const pages=Math.max(1,Math.ceil(total/size)),page=Math.min(requestedPage,pages-1);
  const rows=(await this.c.query(`SELECT d.data,
   CASE WHEN NOT EXISTS(SELECT 1 FROM flowhub_review_decisions newer WHERE newer.owner=d.owner AND newer.sku=d.sku AND newer.seller=d.seller AND (newer.created_at,newer.id)>(d.created_at,d.id) AND NOT (left(newer.id,6)='local-' AND EXISTS(SELECT 1 FROM flowhub_review_decisions remote WHERE left(remote.id,6)!='local-' AND remote.owner=newer.owner AND remote.sku=newer.sku AND remote.seller=newer.seller AND remote.revision=newer.revision AND remote.action=newer.action)))
   THEN jsonb_build_object('listing_state',p.data->'listing_state','listing_error',p.data->'listing_error','target_store_name',p.data->'target_store_name') ELSE '{}'::jsonb END AS extra
   FROM flowhub_review_decisions d ${join} WHERE ${where} ORDER BY d.created_at DESC,d.id DESC LIMIT $3 OFFSET $4`,[owner,q,size,page*size])).rows;
  return {filtered_total:total,pages,page,items:[],history:rows.map(r=>({...r.data,...r.extra}))};
 }
 async upsertProducts(owner,items){
  if(!items.length)return 0;
  return (await this.c.query(`INSERT INTO flowhub_review_products(owner,sku,seller,revision,state,listing_state,search_text,data)
   SELECT $1,x->>'sku',x->>'seller',x->>'revision',coalesce(x->>'pipeline_state',''),x->>'listing_state',
    lower(concat_ws(' ',x->>'sku',x->>'title',x->>'supplier_title',x->>'target_store_name',x->>'note')),x
   FROM jsonb_array_elements($2::jsonb) x
   ON CONFLICT(owner,sku,seller) DO UPDATE SET revision=excluded.revision,state=excluded.state,listing_state=excluded.listing_state,search_text=excluded.search_text,data=excluded.data
   WHERE flowhub_review_products.data IS DISTINCT FROM excluded.data`,[owner,JSON.stringify(items)])).rowCount;
 }
 async removeProducts(owner,removed){
  if(!removed.length)return 0;
  return (await this.c.query(`DELETE FROM flowhub_review_products p USING jsonb_to_recordset($2::jsonb) x(sku text,seller text) WHERE p.owner=$1 AND p.sku=x.sku AND p.seller=x.seller`,[owner,JSON.stringify(removed)])).rowCount;
 }
 async replaceProducts(owner,items){
  const changed=await this.upsertProducts(owner,items);
  const removed=await this.c.query(`DELETE FROM flowhub_review_products p WHERE p.owner=$1 AND NOT EXISTS(SELECT 1 FROM jsonb_to_recordset($2::jsonb) x(sku text,seller text) WHERE x.sku=p.sku AND x.seller=p.seller)`,[owner,JSON.stringify(items.map(({sku,seller})=>({sku,seller})))]);
  return changed+removed.rowCount;
 }
 async importHistory(owner,items){
  if(!items.length)return 0;
  return (await this.c.query(`INSERT INTO flowhub_review_decisions(id,owner,sku,seller,revision,action,status,created_at,data)
   SELECT x->>'id',$1,x->>'sku',x->>'seller',x->>'revision',x->>'action',x->>'status',x->>'created_at',x||jsonb_build_object('owner',$1::text)
   FROM jsonb_array_elements($2::jsonb) x ON CONFLICT(id) DO NOTHING`,[owner,JSON.stringify(items)])).rowCount;
 }
 async decision(id){return (await this.c.query('SELECT data FROM flowhub_review_decisions WHERE id=$1',[id])).rows[0]?.data;}
 async pending(owner){return (await this.c.query("SELECT data FROM flowhub_review_decisions WHERE owner=$1 AND status='pending' ORDER BY created_at,id LIMIT 100",[owner])).rows.map(r=>r.data);}
 async duplicate(owner,item){return !!(await this.c.query("SELECT 1 FROM flowhub_review_decisions WHERE owner=$1 AND sku=$2 AND seller=$3 AND revision=$4 AND status IN ('pending','applied') LIMIT 1",[owner,item.sku,item.seller,item.revision])).rowCount;}
 async ack(item){await this.c.query('UPDATE flowhub_review_decisions SET status=$2,data=$3::jsonb WHERE id=$1',[item.id,item.status,JSON.stringify(item)]);}
}
