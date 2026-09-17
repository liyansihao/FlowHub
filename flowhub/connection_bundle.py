"""Private, password encrypted store transfer; never starts workflows."""
import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from .db import Database


def cipher(password, salt):
    if len(password) < 12:
        raise ValueError('Unlock code must be at least 12 characters')
    return Fernet(base64.urlsafe_b64encode(hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=128*1024*1024)))


def encrypt(payload, password):
    salt = os.urandom(16)
    return json.dumps({'format': 'flowhub-connections-v1', 'salt': salt.hex(), 'data': cipher(password, salt).encrypt(json.dumps(payload).encode()).decode()})


def decrypt(text, password):
    obj = json.loads(text)
    if obj.get('format') != 'flowhub-connections-v1':
        raise ValueError('Unsupported connection file')
    salt = bytes.fromhex(obj['salt'])
    if len(salt) != 16:
        raise ValueError('Invalid salt')
    return json.loads(cipher(password, salt).decrypt(obj['data'].encode()))


def import_stores(database, payload, username='admin'):
    stores = payload['stores']
    if not isinstance(stores, list) or len(stores) > 500:
        raise ValueError('Invalid stores')
    seen = set()
    for s in stores:
        cid = s['credentials']['client_id']
        if not isinstance(cid, str) or not cid.isdigit() or cid in seen:
            raise ValueError('Invalid or duplicate Client ID')
        seen.add(cid)
        if not s['credentials'].get('api_key') or s['kind'] not in ('maozi','ozon'):
            raise ValueError('Missing credentials or invalid store kind')
        if s.get('verified') and (not s['config'].get('warehouse_id') or (s['kind']=='maozi' and (not s['config'].get('shop_id') or not s['credentials'].get('erp_token')))):
            raise ValueError('Incomplete verified binding')
    result = []
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        user = c.execute('SELECT * FROM users WHERE username=? AND active=1', (username,)).fetchone()
        if not user or user['role'] != 'admin':
            raise ValueError('Target must be an active administrator')
        owner = user['id']
        wf = c.execute('SELECT enabled FROM workflows WHERE owner=?',(owner,)).fetchone()
        if wf and wf['enabled']:
            raise ValueError('Pause workflow before importing stores')
        existing = {}
        for row in c.execute('SELECT * FROM stores WHERE owner=?',(owner,)):
            cid = str(database.open(row['secret']).get('client_id',''))
            if cid in existing and cid in seen:
                raise ValueError('Existing duplicate Client ID requires review')
            existing[cid] = row
        for s in stores:
            old = existing.get(s['credentials']['client_id'])
            # Any historical job pins its original binding. Never silently rotate it.
            if old and c.execute('SELECT 1 FROM jobs WHERE store_id=? LIMIT 1',(old['id'],)).fetchone():
                result.append({'name':s['name'],'status':'skipped_has_jobs'});continue
            sid = old['id'] if old else secrets.token_hex(12)
            # Preserve a working connection if the new credentials failed validation.
            if old and old['verified'] and not s.get('verified'):
                result.append({'name':s['name'],'status':'kept_verified_existing'});continue
            enabled = old['enabled'] if old else 0
            c.execute('INSERT INTO stores(id,owner,name,kind,config,secret,enabled,verified,position) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,config=excluded.config,secret=excluded.secret,verified=excluded.verified',
                      (sid,owner,s['name'],s['kind'],json.dumps(s['config']),database.seal(s['credentials']),enabled,int(bool(s.get('verified'))),old['position'] if old else len(existing)+len(result)))
            result.append({'name':s['name'],'status':'updated' if old else 'created','verified':bool(s.get('verified'))})
        database.event(c,owner,'store_import','Imported private store connections; workflow remains paused')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file');parser.add_argument('--username',default='admin')
    args=parser.parse_args()
    try:
        password=getpass.getpass('Connection unlock code: ') if sys.stdin.isatty() else sys.stdin.readline().rstrip('\r\n')
        payload=decrypt(Path(args.file).read_text(),password)
        result=import_stores(Database(),payload,args.username)
        print(json.dumps({'imported':result,'workflow':'paused','new_stores':'disabled; enable and select after reviewing warehouse'},ensure_ascii=False))
    except (ValueError,KeyError,InvalidToken,json.JSONDecodeError):
        print('Import failed: invalid unlock code, invalid configuration, or workflow/account needs review.',file=sys.stderr)
        sys.exit(1)

if __name__=='__main__':main()
