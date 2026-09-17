"""Write a current, factual receipt for the authorized review/listing batch."""
import collections,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from scripts.drive_policy_listings import snapshot,ROOT
D=Database();rows=snapshot(D);latest={}
for line in (ROOT/'results.jsonl').read_text().splitlines():
 r=json.loads(line);latest[(r['sku'],r['seller'])]=r
stats=collections.Counter(r['listing'] for r in rows);classification=collections.Counter(r['result'] for r in latest.values())
summary={'at':time.time(),'checked':len(latest),'classification':dict(classification),'listing':dict(stats),'items':[{k:v for k,v in r.items() if k!='key'} for r in rows]}
(ROOT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
pending=sum(stats[k] for k in ('queued','running','waiting'))
lines=['# 待审核商品按规则上架执行记录','',f"更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}（本机时间）",'',f"检查 {len(latest)} 条记录，包含本次待审核清单及上轮自动通过、等待上架的商品。",'',f"新增上架请求 {len(rows)} 件；已回查确认上架 {stats['listed']} 件，处理中 {pending} 件，受阻 {stats['blocked']} 件。处理中不等于已成功上架。",'', '按用户既定 DINO／千问规则计算同款结论。未达到自动通过条件的记录保留人工审核；已在售复核记录不重复创建商品，本次未新增下架请求。','', '两条泳镜 4671364048、4671428401 已通过同款审核，但 Ozon 返回 DESCRIPTION_DECLINE：图片与商品类型不符。官方类目树中 95330 的确为游泳眼镜，未擅自替换成其他类型来绕过平台拒绝。','', '已复用现有平台警告分类规则恢复仅有非阻塞图片警告的商品。ERROR、拒绝、缺图等硬错误保留；原店额度不足、原价格方案冲突等未通过改店重复发布绕过。','', '| SKU | 上架结果 | 流程状态 | 当前原因 |','|---|---|---|---|']
labels={'listed':'已核验上架','blocked':'受阻','waiting':'处理中','running':'处理中','queued':'排队中'}
for r in rows:
 reason=(r['error'] or (r['pipeline_reason'] if r['listing']=='blocked' else '') or '').replace('|','/').replace('\n',' ')
 lines.append(f"| {r['sku']} | {labels.get(r['listing'],r['listing'])} | {r['pipeline']} | {reason} |")
lines+=['','## 本轮分流统计','']+[f'- {k}: {v}' for k,v in classification.items()]
(ROOT/'执行结果.md').write_text('\n'.join(lines)+'\n')
print(json.dumps({'checked':len(latest),'listing_requested':len(rows),'verified_listed':stats['listed'],'pending':pending,'blocked':stats['blocked']},ensure_ascii=False))
