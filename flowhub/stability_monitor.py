"""Read-only production sampling and durable alert transition journal."""
import argparse
import fcntl
import json
import time
from pathlib import Path

from .stability_snapshot import audit


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def transition(previous, alerts, now):
    current = {a['code'] + ':' + a.get('name', ''): a for a in alerts}
    events = []
    for key, value in current.items():
        if key not in previous or previous[key].get('level') != value.get('level'):
            events.append({'at': now, 'transition': 'opened' if key not in previous else 'severity_changed', **value})
    for key, value in previous.items():
        if key not in current:
            events.append({'at': now, 'transition': 'resolved', **value})
    return current, events


def sample(data, output):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / 'monitor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = output / 'alerts.json'
        previous = json.loads(path.read_text()).get('active', {}) if path.exists() else {}
        try:
            report = audit(data)
            alerts = report['alerts']
            if report.get('paused', {}).get('publication'):
                alerts = [a for a in alerts if a['code'] != 'low_output_with_repair_backlog']
            else:
                low_key='low_output_with_repair_backlog:'
                if (low_key in previous and report['first_stock_verified']['rolling_minutes']['60'] < 15
                        and report['queue'].get('needs_fields',0) >= 8
                        and not any(a['code']=='low_output_with_repair_backlog' for a in alerts)):
                    alerts.append(previous[low_key] | {'count_1h':report['first_stock_verified']['rolling_minutes']['60']})
                pending = sum(report['queue'].get(s, 0) for s in ('queued', 'evaluating', 'publishing', 'awaiting_remote', 'needs_fields'))
                if pending and report['first_stock_verified']['rolling_minutes']['30'] == 0:
                    alerts.append({'level': 'critical', 'code': 'no_output_30m_with_pending_work'})
            report['alerts'] = alerts
            atomic(output / 'latest.json', report)
            with (output / 'samples.jsonl').open('a') as file:
                file.write(json.dumps({k: report[k] for k in ('captured_at', 'queue', 'health', 'first_stock_verified',
                                                             'last_collection_capacity_observation', 'alerts')}, ensure_ascii=False) + '\n')
        except Exception as error:
            # A failed sample cannot prove that existing operational alerts resolved.
            alerts = list(previous.values()) + [{'level': 'critical', 'code': 'monitor_sample_failed', 'error_type': type(error).__name__}]
        current, events = transition(previous, alerts, time.time())
        with (output / 'events.jsonl').open('a') as file:
            for event in events:
                file.write(json.dumps(event, ensure_ascii=False) + '\n')
        atomic(path, {'updated_at': time.time(), 'active': current})
        return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for event in sample(args.data, args.output):
        print(json.dumps(event, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
