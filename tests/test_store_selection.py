import time

import pytest

from flowhub.worker import Worker
from test_acceptance import drain, logged, setup  # noqa: F401


def test_two_computers_share_account_stores_and_cannot_cross_tenants(setup):
    db, admin, user, app = setup
    first, second = logged(app), logged(app)
    other = logged(app, 'alice', 'Alice-Test-Password-2026')
    first.post('/api/stores', json={'name': '共享一号', 'api_key': 'SECRET_NOT_FOR_CLIENT'})
    first.post('/api/stores', json={'name': '共享二号'})
    stores = second.get('/api/stores').json()
    assert len(stores) == 2
    assert 'SECRET_NOT_FOR_CLIENT' not in second.get('/api/stores').text
    sid = stores[1]['id']
    assert other.get('/api/stores').json() == []
    assert other.post('/api/workflow/start', json={'store_ids': [sid]}).status_code == 400
    assert first.post('/api/workflow/start', json={'store_ids': []}).status_code == 400
    assert first.post('/api/workflow/start', json={'store_ids': [sid]}).status_code == 200
    assert second.get('/api/workflow').json()['selected_store_ids'] == [sid]
    assert second.post('/api/workflow/start', json={'store_ids': [sid]}).status_code == 200
    assert second.post('/api/workflow/start', json={'store_ids': [stores[0]['id']]}).status_code == 409
    assert second.post('/api/workflow/pause').status_code == 200
    assert not first.get('/api/workflow').json()['enabled']
    assert first.post('/api/workflow/start').status_code == 200
    assert first.get('/api/workflow').json()['selected_store_ids'] == [sid]


@pytest.mark.asyncio
async def test_only_checked_store_receives_publication(setup):
    db, admin, user, app = setup
    a = logged(app)
    a.post('/api/stores', json={'name': '不勾选'})
    a.post('/api/stores', json={'name': '勾选', 'position': 1})
    sid = a.get('/api/stores').json()[1]['id']
    assert a.post('/api/workflow/start', json={'store_ids': [sid]}).status_code == 200
    w = Worker(db)
    await w.replenish()
    await drain(w)
    with db.connect() as c:
        assert [r[0] for r in c.execute('SELECT DISTINCT store_id FROM jobs WHERE store_id IS NOT NULL')] == [sid]
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE phase='selling'").fetchone()[0] >= 1


@pytest.mark.asyncio
async def test_deselected_unsubmitted_waits_and_submitted_keeps_original_store(setup):
    db, admin, user, app = setup
    a = logged(app)
    for name in ['first', 'second']:
        a.post('/api/stores', json={'name': name})
    ids = [s['id'] for s in a.get('/api/stores').json()]
    a.post('/api/workflow/start', json={'store_ids': [ids[0]]})
    w = Worker(db)
    await w.replenish()
    prepared = None
    for _ in range(30):
        with db.connect() as c:
            c.execute('UPDATE jobs SET next_at=0')
            prepared = c.execute("SELECT id FROM jobs WHERE phase='prepared'").fetchone()
        if prepared:
            break
        await w.step()
    assert prepared
    a.post('/api/workflow/pause')
    assert a.post('/api/workflow/start', json={'store_ids': [ids[1]]}).status_code == 200
    await drain(w, 8)
    with db.connect() as c:
        row = c.execute('SELECT phase,store_id FROM jobs WHERE id=?', (prepared[0],)).fetchone()
        assert tuple(row) == ('prepared', ids[0])
        assert c.execute('SELECT 1 FROM demo_effects WHERE id=?', (prepared[0],)).fetchone() is None
    a.post('/api/workflow/pause')
    assert a.post('/api/workflow/start', json={'store_ids': [ids[0]]}).status_code == 200
    with db.connect() as c:
        c.execute('UPDATE jobs SET next_at=?', (time.time() + 600,))
        c.execute('UPDATE jobs SET next_at=0 WHERE id=?', (prepared[0],))
    await w.step()
    a.post('/api/workflow/pause')
    assert a.post('/api/workflow/start', json={'store_ids': [ids[1]]}).status_code == 200
    await drain(w, 30)
    with db.connect() as c:
        row = c.execute('SELECT phase,store_id FROM jobs WHERE id=?', (prepared[0],)).fetchone()
        assert tuple(row) == ('selling', ids[0])
