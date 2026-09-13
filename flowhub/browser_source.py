"""Browser-only list transport boundary. No product-detail or publication operations.

A caller keeps its Playwright session, navigates next_request()['url'], and saves page
source. This module validates that source and atomically commits SKU + cursor.
It deliberately does not launch a browser or claim an unattended UI worker.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from .db import Database
from .source_library import SourceLibrary
from .storefront import parse_packet, shop_url, StorefrontError


class BrowserSource:
    def __init__(self, db):
        self.db=db
        self.library=SourceLibrary(db)
        with db.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS browser_source_scans(
             owner TEXT,run_id TEXT,seller TEXT,roots TEXT,next_url TEXT,page INTEGER,
             state TEXT,browser TEXT,updated REAL,PRIMARY KEY(owner,run_id,seller));
            CREATE TABLE IF NOT EXISTS browser_source_pages(
             owner TEXT,run_id TEXT,seller TEXT,page INTEGER,digest TEXT,content_hash TEXT,
             body TEXT,at REAL,PRIMARY KEY(owner,run_id,seller,page));
            CREATE TABLE IF NOT EXISTS browser_source_failures(
             owner TEXT,run_id TEXT,seller TEXT,page INTEGER,reason TEXT,at REAL);
            ''')

    def prepare(self, owner, run_id, manifest):
        with self.db.connect() as c:
            for seller,roots in manifest.items():
                url=shop_url(f'/seller/{seller}/products/',seller)
                if not roots:raise ValueError('root_seeds_required')
                c.execute('INSERT OR IGNORE INTO browser_source_scans VALUES(?,?,?,?,?,1,?,?,?)',
                          (owner,run_id,str(seller),json.dumps(roots),url,'paused','playwright',time.time()))

    def control(self, owner, run_id, seller, action):
        if action not in ('pause','resume','retry'):raise ValueError('invalid_action')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT state FROM browser_source_scans WHERE owner=? AND run_id=? AND seller=?',(owner,run_id,seller)).fetchone()
            if not r:raise KeyError(seller)
            if r[0]=='done':return
            if r[0]=='blocked' and action=='resume':raise ValueError('retry_required')
            c.execute('UPDATE browser_source_scans SET state=?,updated=? WHERE owner=? AND run_id=? AND seller=?',
                      ('paused' if action=='pause' else 'ready',time.time(),owner,run_id,seller))

    def fail(self, owner, run_id, seller, reason):
        # Browser transport errors preserve the current page and require explicit retry.
        if not reason or len(reason)>120:raise ValueError('invalid_reason')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT page,state FROM browser_source_scans WHERE owner=? AND run_id=? AND seller=?',(owner,run_id,seller)).fetchone()
            if not r:raise KeyError(seller)
            if r['state']=='ready':
                c.execute('INSERT INTO browser_source_failures VALUES(?,?,?,?,?,?)',(owner,run_id,seller,r['page'],reason,time.time()))
                c.execute("UPDATE browser_source_scans SET state='blocked',updated=? WHERE owner=? AND run_id=? AND seller=?",(time.time(),owner,run_id,seller))
        return {'state':'blocked' if r['state']=='ready' else r['state'],'reason':reason,'page':r['page']}

    def next_request(self, owner, run_id, seller):
        with self.db.connect() as c:
            r=c.execute('SELECT * FROM browser_source_scans WHERE owner=? AND run_id=? AND seller=?',(owner,run_id,seller)).fetchone()
            paused=c.execute("SELECT paused FROM pipeline_module_control WHERE module='seed'").fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_module_control'").fetchone() else None
        if not r:raise KeyError(seller)
        state='paused' if paused and paused[0] else r['state']
        return {'state':state,'browser':r['browser'],'seller':seller,'page':r['page'],
                'url':r['next_url'] if state=='ready' else None,'run_id':run_id}

    def ingest(self, owner, run_id, seller, requested_url, html, artifact):
        digest=hashlib.sha256(html.encode()).hexdigest();now=time.time();key=(owner,run_id,seller)
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            task=c.execute('SELECT * FROM browser_source_scans WHERE owner=? AND run_id=? AND seller=?',key).fetchone()
            if not task:raise KeyError(seller)
            replay=c.execute('SELECT 1 FROM browser_source_pages WHERE owner=? AND run_id=? AND seller=? AND digest=?',(*key,digest)).fetchone()
            if replay:return {'state':'replay','added':0}
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_module_control'").fetchone():
                paused=c.execute("SELECT paused FROM pipeline_module_control WHERE module='seed'").fetchone()
                if paused and paused[0]:raise StorefrontError('task_paused')
            if task['state']!='ready':raise StorefrontError('task_'+task['state'])
            try:
                if shop_url(requested_url,seller,urlsplit(task['next_url']).path)!=task['next_url']:raise StorefrontError('checkpoint_mismatch')
                packet=parse_packet(html,seller,requested_url)
                if packet['page']!=task['page']:raise StorefrontError('page_mismatch')
                if not -300<=now-packet['observed_at']<=21600:raise StorefrontError('stale_page')
                ids=sorted(p['sku'] for p in packet['products']);content=hashlib.sha256(','.join(ids).encode()).hexdigest()
                if ids and c.execute('SELECT 1 FROM browser_source_pages WHERE owner=? AND run_id=? AND seller=? AND content_hash=?',(*key,content)).fetchone():raise StorefrontError('repeated_page')
            except StorefrontError as e:
                c.execute('INSERT INTO browser_source_failures VALUES(?,?,?,?,?,?)',(*key,task['page'],str(e),now))
                c.execute("UPDATE browser_source_scans SET state='blocked',updated=? WHERE owner=? AND run_id=? AND seller=?",(now,*key))
                return {'state':'blocked','reason':str(e),'added':0}
            roots=json.loads(task['roots']);added=0
            evidence={'channel':'browser-page-source','browser':'playwright','run_id':run_id,'seller_id':seller,
                      'requested_url':requested_url,'artifact':artifact,'observed_at':packet['observed_at'],
                      'imported_at':now,'sha256':digest,'root_seeds':roots}
            for p in packet['products']:
                if c.execute('SELECT 1 FROM sourcing_products WHERE owner=? AND sku=? AND seller=?',(owner,p['sku'],seller)).fetchone():continue
                p['source_relation']={'kind':'same_seller','seller_id':seller,'root_seeds':roots}
                added+=self.library.put(owner,p,evidence,connection=c)
            evidence.update(skus=ids,added=added,next_url=packet['next_url'],explicit_end=packet['explicit_end'])
            c.execute('INSERT INTO browser_source_pages VALUES(?,?,?,?,?,?,?,?)',(*key,packet['page'],digest,content,json.dumps(evidence),now))
            c.execute('UPDATE browser_source_scans SET page=page+1,next_url=?,state=?,updated=? WHERE owner=? AND run_id=? AND seller=?',
                      (packet['next_url'],'done' if packet['explicit_end'] else 'ready',now,*key))
            return {'state':'committed','rows':len(ids),'added':added,'page':packet['page'],'next_url':packet['next_url']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['next','ingest','pause','resume','retry','fail'])
    p.add_argument('--owner',required=True);p.add_argument('--run-id',required=True);p.add_argument('--seller',required=True)
    p.add_argument('--url');p.add_argument('--html');p.add_argument('--reason')
    a=p.parse_args();service=BrowserSource(Database());key=(a.owner,a.run_id,a.seller)
    if a.action=='fail':result=service.fail(*key,a.reason)
    elif a.action=='ingest':
        if not a.url or not a.html:p.error('ingest requires --url and --html')
        file=Path(a.html);result=service.ingest(*key,a.url,file.read_text(),str(file.resolve()))
    else:
        if a.action!='next':service.control(*key,a.action)
        result=service.next_request(*key)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
