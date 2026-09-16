"""Read-only fixed-window measurements; count each publication's first stock verification once."""
import argparse
import collections
import json
import sqlite3
import statistics
import time
from pathlib import Path


def measure(data, start, end):
    counts=collections.Counter(); timings=[]; latency=[]; requests=collections.Counter()
    with sqlite3.connect('file:'+str(data/'flowhub.sqlite3')+'?mode=ro',uri=True) as c:
        for raw, in c.execute('SELECT body FROM plugin_publications'):
            p=json.loads(raw)
            verified=[e['at'] for e in p.get('events',[]) if e.get('to')=='stock_verified']
            if verified and start <= min(verified) < end:
                counts['first_stock_verified']+=1
                reviewed=p.get('review',{}).get('finished_at')
                if isinstance(reviewed,(int,float)) and 0 < reviewed <= min(verified):
                    latency.append(min(verified)-reviewed)
            for e in p.get('events',[]):
                if start <= e.get('at',0) < end and e.get('error'):
                    counts['publication_error_events']+=1
            for block in p.get('api_timings',[]):
                if not start <= block.get('at',0) < end:continue
                for call in block.get('calls',[]):
                    requests[call['path']]+=1
                    counts['cache_hits']+=bool(call.get('cache_hit'))
                    counts['coalesced_reads']+=bool(call.get('coalesced'))
                    if 'network_ms' in call:timings.append(call)
        repairs=[]
        for raw, in c.execute('SELECT body FROM plugin_repair_events WHERE at>=? AND at<?',(start,end)):
            e=json.loads(raw);repairs.append(e)
        counts['repair_executions']=len(repairs)
        counts['repair_complete']=sum(not e.get('after') for e in repairs)
        counts['local_draft_reuses']=sum(any(s.get('source')=='maozi-erp-draft-cache' for s in e.get('steps',[])) for e in repairs)
        states=dict(c.execute('SELECT state,count(*) FROM plugin_pipeline GROUP BY state'))
    duration=end-start
    return {'start':start,'end':end,'duration_seconds':duration,'counts':dict(counts),
            'verified_per_hour':round(counts['first_stock_verified']*3600/duration,2),
            'review_to_verified_seconds':{'samples':len(latency),'median':round(statistics.median(latency),2) if latency else None},
            'publication_request_counts':dict(requests),'measured_requests':len(timings),
            'timing_medians_ms':{k:round(statistics.median(r[k] for r in timings if k in r),2) for k in ('pacing_wait_ms','network_ms','pool_wait_ms') if any(k in r for r in timings)},
            'pipeline_states_at_read':states}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=Path(__file__).resolve().parents[1]/'data')
    parser.add_argument('--seconds',type=int,default=900)
    parser.add_argument('--output',type=Path,required=True)
    a=parser.parse_args()
    if a.seconds<=0:parser.error('--seconds must be positive')
    start=time.time();a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.with_suffix('.start.json').write_text(json.dumps({'start':start,'planned_seconds':a.seconds}))
    print(json.dumps({'measurement_started':start,'seconds':a.seconds}),flush=True)
    while time.time()<start+a.seconds:
        time.sleep(min(30,max(0,start+a.seconds-time.time())))
    report=measure(a.data,start,start+a.seconds)
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False),flush=True)
