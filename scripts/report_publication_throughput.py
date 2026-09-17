"""Read-only rolling throughput and request timing report from durable publications."""
import argparse,collections,json,sqlite3,statistics,time
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=Path,default=Path(__file__).resolve().parents[1]/'data');p.add_argument('--output',type=Path,required=True);p.add_argument('--since',type=float);a=p.parse_args()
c=sqlite3.connect('file:'+str(a.data/'flowhub.sqlite3')+'?mode=ro',uri=True);now=time.time();since=a.since or now-3600
counts={str(m):0 for m in (15,60,180)};calls=collections.defaultdict(list);success=[];errors=collections.Counter();measured=[]
for b, in c.execute('select body from plugin_publications'):
 d=json.loads(b);es=d.get('events',[]);verified=[e['at'] for e in es if e.get('to')=='stock_verified']
 if verified:
  at=min(verified)
  for m in (15,60,180):counts[str(m)]+=int(at>=now-m*60)
  if at>=since:success.append(at)
 for e in es:
  if e.get('at',0)>=since and e.get('error'):errors[e.get('error')]+=1
 for block in d.get('api_timings',[]):
  if block.get('at',0)<since:continue
  for v in block.get('calls',[]):
   calls[v['path']].append(v)
   if 'network_ms' in v:measured.append(v)
report={'at':now,'since':since,'first_stock_verified_counts_by_minutes':counts,'new_verified_since':len(success),
 'pipeline_states':dict(c.execute('select state,count(*) from plugin_pipeline group by state')),
 'errors':dict(errors),'request_counts':{k:{'total':len(v),'cached':sum(bool(x.get('cache_hit')) for x in v),'coalesced':sum(bool(x.get('coalesced')) for x in v)} for k,v in calls.items()},
 'measured_count':len(measured),'timing_medians_ms':{key:round(statistics.median(v[key] for v in measured if key in v),2) for key in ('pool_wait_ms','pacing_wait_ms','network_ms','connect_ms') if any(key in v for v in measured)}}
a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
