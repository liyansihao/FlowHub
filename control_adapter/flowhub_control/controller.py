"""Whitelisted controls; never starts an alternate publisher or deletes work."""
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

PRODUCTION = Path('/Users/mac/Desktop/ozon/FlowHub')
LABEL = 'com.flowhub.production'
COMMIT = '7da5b6d47c3b6dc0d234ce691d9e11e2c9eaf9f9'
SOURCE = 'f59fb378418ab9dcadcc34e73ec0aa811915e36a09fbac1bc9aa6cb6849f11a7'


def state_version(health):
    value = [health.get('instance_id'), health.get('controls'), health.get('service_loaded')]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class MacService:
    def __init__(self, root=PRODUCTION):
        if Path(root).resolve() != PRODUCTION: raise ValueError('sole_production_path_required')
        self.root = PRODUCTION
        self.target = f'gui/{os.getuid()}/{LABEL}'
        self.plist = Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'

    def loaded(self):
        return subprocess.run(['launchctl','print',self.target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0

    def validate(self):
        config = plistlib.loads(self.plist.read_bytes())
        args = config.get('ProgramArguments', [])
        if config.get('Label') != LABEL or Path(config.get('WorkingDirectory', '')).resolve() != self.root:
            raise ValueError('launchagent_identity_mismatch')
        expected=['/usr/bin/caffeinate','-is',str(self.root/'.venv/bin/python'),'-m','flowhub.service_entry']
        if args != expected or config.get('EnvironmentVariables',{}).get('FLOWHUB_DATA') != str(self.root/'data'):
            raise ValueError('launchagent_command_mismatch')
        revision = subprocess.check_output(['git','rev-parse','HEAD'], cwd=self.root, text=True, timeout=5).strip()
        if revision != COMMIT: raise ValueError('production_version_mismatch')
        # Verify tracked files against the frozen tag, without importing production modules.
        dirty = subprocess.run(['git','diff','--quiet',COMMIT,'--','flowhub'], cwd=self.root, timeout=10)
        if dirty.returncode: raise ValueError('production_source_drift')
        unknown = subprocess.check_output(['git','ls-files','--others','--exclude-standard','flowhub'], cwd=self.root, text=True, timeout=5)
        if unknown.strip(): raise ValueError('untracked_production_source')
        if shutil.disk_usage(self.root).free < 3*1024**3: raise ValueError('insufficient_disk_space')

    def start(self):
        self.validate()
        if not self.loaded():
            # bootstrap existing sole service, never invoke generic control start.
            subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(self.plist)], check=True, capture_output=True, timeout=15)


class Controller:
    def __init__(self, reader, store, service, enabled=False, clock=time.time, drain_seconds=600):
        self.reader,self.store,self.service,self.enabled,self.clock = reader,store,service,enabled,clock
        self.drain_seconds = drain_seconds

    def status(self):
        health = self.reader.health()
        health['service_loaded'] = self.service.loaded()
        health['state_version'] = state_version(health)
        health['controls_enabled'] = self.enabled
        health['queue_counts'] = {r['state']:r['n'] for r in self.reader.query('SELECT state,count(*) n FROM plugin_pipeline GROUP BY state')}
        health['stop_capability'] = 'drain_then_require_attention_without_core_quiescence_barrier'
        return health

    def pause(self, command_id):
        original = self.reader.controls().get('seed', {'paused': False, 'updated': None})
        if original['paused']: return
        intent = self.store.get('owned_pause')
        if intent is None:
            intent = {'before': original, 'updated': self.clock(), 'command_id': command_id}
            self.store.put('owned_pause', intent)  # durable before production side effect
        with sqlite3.connect(self.reader.data/'flowhub.sqlite3', timeout=.25) as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT paused,updated FROM pipeline_module_control WHERE module='seed'").fetchone()
            current = {'paused': bool(row[0]), 'updated': row[1]} if row else {'paused': False, 'updated': None}
            if current == {'paused': True, 'updated': intent['updated']}: return
            if current != intent['before']: raise ValueError('pause_conflict')
            c.execute("INSERT OR REPLACE INTO pipeline_module_control(module,paused,updated) VALUES('seed',1,?)", (intent['updated'],))

    def resume(self):
        intent = self.store.get('owned_pause')
        current = self.reader.controls().get('seed', {'paused': False, 'updated': None})
        if not current['paused']:
            self.store.put('owned_pause', None)
            return
        if not intent or current != {'paused': True, 'updated': intent['updated']}:
            raise ValueError('pause_not_owned_or_changed_locally')
        resume_stamp = intent.get('resume_updated') or self.clock()
        intent['resume_updated'] = resume_stamp
        self.store.put('owned_pause', intent)
        with sqlite3.connect(self.reader.data/'flowhub.sqlite3', timeout=.25) as c:
            c.execute('BEGIN IMMEDIATE')
            changed=c.execute("UPDATE pipeline_module_control SET paused=0,updated=? WHERE module='seed' AND paused=1 AND updated=?",(resume_stamp,intent['updated'])).rowcount
            if changed != 1: raise ValueError('resume_conflict')
        self.store.put('owned_pause', None)

    def drain_blockers(self):
        now = self.clock()
        checks = {
            'active_pipeline_leases': ('SELECT count(*) n FROM plugin_pipeline_leases WHERE expires>?', (now,)),
            'unfinished_products': ("SELECT count(*) n FROM plugin_pipeline WHERE state NOT IN ('selling','rejected','failed','quarantined','delisted','not_listed','same_product_confirmed') OR state IS NULL",()),
            'source_leases': ('SELECT count(*) n FROM sourcing_tasks WHERE lease_until>?', (now,)),
            'acquisition_leases': ('SELECT count(*) n FROM acquisition_tasks WHERE lease_until>?', (now,)),
            'pending_listing_controls': ("SELECT count(*) n FROM product_listing_controls WHERE state NOT IN ('done','applied','failed','cancelled')",()),
        }
        blockers = {}
        for name, (sql, args) in checks.items():
            try:
                count = self.reader.query(sql,args)[0]['n']
                if count: blockers[name] = count
            except sqlite3.Error: blockers[name] = 'unavailable'
        # A snapshot cannot fence an uninstrumented lane or a remote unknown write.
        # Without changing v1.0-stable, safe automatic bootout cannot be proven.
        blockers['core_quiescence_barrier'] = 'unavailable_in_v1.0_stable'
        return blockers

    def execute(self, cmd):
        previous = self.store.begin_command(cmd)
        if previous and previous['status'] in ('applied','rejected','expired','needs_attention'): return previous
        def finish(status, reason, **extra):
            result={'id': cmd['id'], 'status': status, 'reason': reason, 'at': self.clock(), **extra}
            self.store.result(cmd['id'],result)
            return result
        try:
            if not self.enabled: return finish('rejected','controls_disabled')
            if cmd.get('action') not in ('start','resume','pause','stop'): return finish('rejected','unknown_action')
            if self.clock()>cmd['expires_at']:
                return finish('needs_attention' if previous else 'expired','command_expired_no_force_stop')
            health = self.status()
            if not previous:
                if cmd.get('expected_state_version') != health['state_version']: return finish('rejected','state_conflict')
                if cmd.get('target_revision') != COMMIT: return finish('rejected','version_conflict')
                self.service.validate()
                self.store.result(cmd['id'], {'id':cmd['id'],'status':'running','at':self.clock()})
            self.service.validate()
            if cmd['action'] == 'pause':
                self.pause(cmd['id'])
                return finish('applied','new_admission_paused_readback_continues')
            if cmd['action'] in ('start','resume'):
                campaigns = self.reader.query('SELECT owner FROM pipeline_campaigns WHERE enabled=1 LIMIT 1')
                if not campaigns: return finish('rejected','no_existing_enabled_campaign')
                self.service.validate()
                # Do not restart a loaded but unhealthy worker, let its existing supervisor act.
                if health['service_loaded'] and not health['heartbeat_fresh'] and previous and previous.get('reason')=='waiting_for_worker_heartbeat':
                    return finish('running','waiting_for_worker_heartbeat')
                if health['service_loaded'] and not health['heartbeat_fresh']:
                    return finish('needs_attention','loaded_service_not_healthy')
                if not health['service_loaded'] and health['pid_alive']:
                    return finish('needs_attention','worker_outside_expected_service')
                self.resume()
                self.service.start()
                observed = self.status()
                if observed['heartbeat_fresh'] and observed['pid_alive'] and observed['service_loaded']:
                    return finish('applied','existing_plan_running')
                return finish('running','waiting_for_worker_heartbeat')
            if not health['pid_alive'] and not health['service_loaded']:
                return finish('applied','already_stopped')
            self.pause(cmd['id'])
            started = (previous or {}).get('drain_started', self.clock())
            blockers = self.drain_blockers()
            if self.clock()-started >= self.drain_seconds:
                return finish('needs_attention','safe_stop_not_proven_readback_preserved', blockers=blockers, drain_started=started)
            return finish('draining','waiting_for_safe_boundary', blockers=blockers, drain_started=started)
        except (ValueError, sqlite3.Error, OSError, subprocess.SubprocessError) as e:
            # Whitelist reason, never include paths, raw exception messages or request credentials.
            reason = str(e) if isinstance(e, ValueError) else type(e).__name__
            return finish('needs_attention', reason)
