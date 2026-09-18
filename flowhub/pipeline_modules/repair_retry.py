"""Separate unavailable dependencies from missing/invalid product facts."""
import json


def classify(evidence):
    steps=evidence.get('steps',[])
    text=json.dumps(steps,ensure_ascii=False).lower()
    if any(x in text for x in ('connecttimeout','connecterror','connect_timeout','readtimeout','remoteprotocolerror','readerror','writeerror','pooltimeout','econnreset','enotfound','fetch failed','maozi_api_pacing_wait','maozi_pacing_lock_timeout','timeout_error','timeouterror','etimedout','rate_limited')):
        return 'network'
    if any(x in text for x in ('write_outcome_unknown": true','claim changed','outcome unknown','source request pending','source acquisition pending','draft outcome unresolved','favorite creation not confirmed','recovery listing incomplete')):
        return 'remote_pending'
    if any(x in text for x in ('sku_mismatch','draft_source_mismatch','ambiguous_draft_variant','wrong_seller')):
        return 'identity_mismatch'
    if any(x in text for x in ('采集箱已满','collection_box_full')):return 'capacity'
    return 'missing_fields'


def schedule(repair, result, now):
    kind=result.get('failure_class','missing_fields')
    repair.update(failure_class=kind,last_attempt_at=now,missing_fields=result.get('missing_fields',[]),reason=result['reason'])
    repair['total_attempts']=repair.get('total_attempts',0)+1
    if kind in ('network','remote_pending'):
        repair.setdefault('dependency_since',now)
        repair['dependency_attempts']=repair.get('dependency_attempts',0)+1
        delay=min(900,60*2**min(repair['dependency_attempts']-1,4))
        exhausted=False
    else:
        repair.pop('dependency_since',None);repair.pop('dependency_attempts',None)
        repair['attempts']=int(repair.get('attempts',0))+1
        repair.setdefault('first_attempt_at',now)
        delay=min(3600,300*2**min(repair['attempts']-1,4))
        exhausted=repair['attempts']>=6
    repair['delay_seconds']=delay
    return delay,exhausted
