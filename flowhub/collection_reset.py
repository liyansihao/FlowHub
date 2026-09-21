"""Explicit collection-box maintenance. Never invokes online or stock APIs.

Run with --execute to archive and empty the selected account's draft box.
Existing deletion intents are only reconciled, never replayed.
"""
import argparse
import asyncio
import fcntl
import json
import sqlite3
import time
from pathlib import Path

from .db import Database
from .collection_capacity import account, observe
from .pipeline_modules import control
from .pipeline_modules.draft_cleanup import Client, listing, journal, schema


async def read(operation):
    for attempt in range(3):
        try:
            return await operation()
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)


async def reconcile(db, scope, client):
    started = time.time()
    header, remote = await read(lambda: listing(client))
    present = {str(row['id']) for row in remote}
    with db.connect() as c:
        receipts = c.execute("SELECT * FROM draft_cleanup_receipts WHERE account=? AND state!='deleted'", (scope,)).fetchall()
        for row in receipts:
            if row['draft_id'] not in present:
                c.execute("UPDATE draft_cleanup_receipts SET state='deleted',updated=? WHERE account=? AND draft_id=?",
                          (time.time(), scope, row['draft_id']))
    observe(db, scope, header, started)
    return header, remote


async def clear(db, context, client=None, progress=print):
    """Caller must hold maintenance locks and drain all source-dependent work."""
    schema(db)
    scope = account(context)
    client = client or Client(context)
    header, remote = await reconcile(db, scope, client)
    initial = len(remote)
    # Freeze the scope at the initial complete listing. New arrivals are not swept.
    with db.connect() as c:
        prior = {r[0] for r in c.execute('SELECT draft_id FROM draft_cleanup_receipts WHERE account=?', (scope,))}
    pending = [row for row in remote if str(row['id']) not in prior]
    for offset in range(0, len(pending), 20):
        batch = pending[offset:offset + 20]
        backups = []
        for row in batch:
            identifier = str(row['id'])
            if not identifier.isdigit() or int(identifier) <= 0:
                raise ValueError('invalid draft identity')
            detail = await read(lambda: client.call('/api.product.collect/detail', params={'id':int(identifier), 'is_online':0}))
            if not isinstance(detail, dict) or not isinstance(detail.get('skus'), list) or not detail['skus']:
                raise ValueError('incomplete source backup; no deletion')
            backups.append((row, {'draft_row':row, 'fresh_detail':detail, 'at':time.time(),
                                 'scope':'explicit-collection-box-reset', 'authorization':'2026-09-18 用户要求采集箱清零'}))
        for row, backup in backups:
            identifier = str(row['id'])
            with db.connect() as c:
                if any(not control.paused(db, m) for m in control.MODULES):
                    raise RuntimeError('maintenance pause changed; stop deleting')
                inserted = c.execute('INSERT OR IGNORE INTO draft_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
                    (scope, identifier, context['owner'], str(row.get('goods_id','')), 'intent', db.seal(backup), time.time())).rowcount
            if not inserted:
                continue
            try:
                backup['response'] = await client.call('/api.product.collect/del', 'DELETE', {'ids':identifier})
                state = 'acknowledged'
            except Exception as error:
                backup['error_type'] = type(error).__name__
                state = 'unconfirmed'
            journal(db, scope, identifier, context['owner'], str(row.get('goods_id','')), state, backup)
        header, remaining = await reconcile(db, scope, client)
        progress(json.dumps({'initial':initial, 'processed':min(offset+20,len(pending)), 'remaining':len(remaining)}, ensure_ascii=False))
    header, remaining = await reconcile(db, scope, client)
    return {'initial':initial, 'remaining':len(remaining), 'used':header.get('used'),
            'complete':len(remaining)==0 and int(header.get('used', -1))==0, 'finished_at':time.time()}


async def retry_authorized(db, context, identifiers, authorization_id, client=None):
    """One additional attempt on exact IDs only after a separate explicit approval.

    Caller holds the same maintenance locks as clear(). An approval cannot be
    reused to replay an uncertain result. Every original receipt is preserved.
    """
    if not authorization_id or not identifiers:raise ValueError('explicit approval and exact IDs required')
    schema(db)
    with db.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS collection_reset_overrides(
            authorization TEXT,account TEXT,draft_id TEXT,previous_receipt TEXT,created REAL,
            PRIMARY KEY(authorization,account,draft_id))''')
    scope=account(context);client=client or Client(context)
    _,remote=await reconcile(db,scope,client)
    present={str(r['id']):r for r in remote}
    for identifier in dict.fromkeys(str(i) for i in identifiers):
        if identifier not in present:continue
        with db.connect() as c:
            old=c.execute('SELECT * FROM draft_cleanup_receipts WHERE account=? AND draft_id=?',(scope,identifier)).fetchone()
            used=c.execute('SELECT 1 FROM collection_reset_overrides WHERE authorization=? AND account=? AND draft_id=?',
                           (authorization_id,scope,identifier)).fetchone()
        if used:continue
        if (not old or old['state']!='unconfirmed' or old['owner']!=context['owner']
                or old['sku']!=str(present[identifier].get('goods_id'))):
            raise ValueError('authorized receipt identity/state mismatch')
        detail=await read(lambda:client.call('/api.product.collect/detail',params={'id':int(identifier),'is_online':0}))
        if not isinstance(detail,dict) or not detail.get('skus'):raise ValueError('incomplete source backup')
        backup={'draft_row':present[identifier],'fresh_detail':detail,'at':time.time(),
                'scope':'explicit-authorized-retry','authorization_id':authorization_id,'previous_receipt':dict(old)}
        with db.write_transaction() as c:
            if any(not c.execute('SELECT paused FROM pipeline_module_control WHERE module=?',(m,)).fetchone()[0] for m in control.MODULES):
                raise RuntimeError('maintenance pause changed')
            inserted=c.execute('INSERT OR IGNORE INTO collection_reset_overrides VALUES(?,?,?,?,?)',
                (authorization_id,scope,identifier,db.seal(dict(old)),time.time())).rowcount
            if not inserted:continue
            c.execute("UPDATE draft_cleanup_receipts SET state='intent',body=?,updated=? WHERE account=? AND draft_id=?",
                      (db.seal(backup),time.time(),scope,identifier))
        try:
            backup['response']=await client.call('/api.product.collect/del','DELETE',{'ids':identifier})
            state='acknowledged'
        except Exception as error:
            backup['error_type']=type(error).__name__;state='unconfirmed'
        journal(db,scope,identifier,context['owner'],old['sku'],state,backup)
    header,remaining=await reconcile(db,scope,client)
    return {'remaining':len(remaining),'used':header.get('used'),
            'complete':not remaining and int(header.get('used',-1))==0,'finished_at':time.time()}


async def maintenance(db, context, *, automatic=False):
    locks=[]
    control.schema(db)
    receipt_path = db.directory/'collection-reset-status.json'
    with db.connect() as c:
        prior = dict(c.execute('SELECT module,paused FROM pipeline_module_control'))
    receipt = {'started_at':time.time(), 'prior_paused':prior, 'scope':account(context), 'state':'draining'}
    def save():
        tmp=receipt_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2));tmp.chmod(0o600);tmp.replace(receipt_path)
    marks={}
    try:
        singleton=(db.directory/'collection-reset.lock').open('a');locks.append(singleton)
        fcntl.flock(singleton,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # A crashed maintenance run leaves pauses in place; never guess prior intent.
        if receipt_path.exists() and json.loads(receipt_path.read_text()).get('state') in ('draining','clearing'):
            raise RuntimeError('unfinished maintenance: inspect saved pauses before resume')
        with db.write_transaction() as c:
            prior=dict(c.execute('SELECT module,paused FROM pipeline_module_control'))
            if automatic and any(prior.values()):return {'state':'paused'}
            receipt['prior_paused']=prior
            for module in control.MODULES:
                marks[module]=time.time()
                c.execute('INSERT OR REPLACE INTO pipeline_module_control VALUES(?,?,?)',(module,1,marks[module]))
            receipt['pause_marks']=marks;receipt['automatic']=automatic
            save()
        deadline=time.monotonic()+300
        while True:
            with db.connect() as c:
                leases=c.execute('SELECT count(*) FROM plugin_pipeline_leases WHERE expires>?',(time.time(),)).fetchone()[0]
                jobs=c.execute('SELECT count(*) FROM jobs WHERE lease_until>?',(time.time(),)).fetchone()[0]
                # New acquisition leases survive between legacy pipeline ticks.
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_tasks'").fetchone():
                    leases+=c.execute('SELECT count(*) FROM acquisition_tasks WHERE account=? AND lease_until>?',
                                      (account(context),time.time())).fetchone()[0]
            commands=0
            cluster=db.directory/'cluster/cluster.sqlite3'
            if cluster.exists():
                with sqlite3.connect(cluster.as_uri()+'?mode=ro',uri=True,timeout=10) as c:
                    commands=c.execute("SELECT count(*) FROM erp_commands WHERE state IN ('queued','claimed','executing') AND deadline>?",(time.time(),)).fetchone()[0]
            if not leases and not jobs and not commands:break
            if time.monotonic()>deadline:raise TimeoutError('active source work did not drain')
            await asyncio.sleep(1)
        for name in ('plugin-publication.lock','source-loop.lock','draft-cleanup.lock','favorite-cleanup.lock'):
            lock=(db.directory/name).open('a');locks.append(lock)
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise TimeoutError('maintenance lock busy')
                    await asyncio.sleep(1)
        receipt['state']='clearing';save()
        receipt['result']=await clear(db,context,progress=lambda value:print(value,flush=True))
        receipt['state']='complete' if receipt['result']['complete'] else 'needs_reconciliation'
    except BaseException as error:
        receipt.update(state='interrupted',error_type=type(error).__name__)
        raise
    finally:
        for module,updated in marks.items():
            with db.connect() as c:
                c.execute('UPDATE pipeline_module_control SET paused=?,updated=? WHERE module=? AND paused=1 AND updated=?',
                          (prior.get(module,0),time.time(),module,updated))
        for lock in reversed(locks):lock.close()
        if marks:
            receipt['finished_at']=time.time();save()
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if not args.execute:parser.error('--execute is required for explicitly authorized maintenance')
    # Maintenance opens the existing database without running application migrations.
    from cryptography.fernet import Fernet
    db=Database.__new__(Database)
    db.directory=args.data.resolve();db.path=db.directory/'flowhub.sqlite3'
    if not db.path.is_file():raise ValueError('existing production database required')
    db.cipher=Fernet((db.directory/'master.key').read_bytes())
    with db.connect() as c:stores=c.execute('SELECT * FROM stores WHERE enabled=1').fetchall()
    contexts={}
    for row in stores:
        context={'owner':row['owner'],'store':{'config':json.loads(row['config']),'credentials':db.open(row['secret'])}}
        if context['store']['credentials'].get('erp_token'):contexts[account(context)]=context
    if len(contexts)!=1:raise ValueError('maintenance requires exactly one account; no automatic account expansion')
    print(json.dumps(asyncio.run(maintenance(db,next(iter(contexts.values())))),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
