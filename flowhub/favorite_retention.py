"""Compatibility guard for independent drafts retained after favorite release.

The release worker is not part of main, but its certificates remain authoritative.
Keep every certified draft until a separately reviewed data migration says otherwise.
"""


def draft_retained(connection, draft_id):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='favorite_release_certificates'"
    ).fetchone():
        return False
    return bool(connection.execute(
        'SELECT 1 FROM favorite_release_certificates WHERE draft_id=? LIMIT 1',
        (str(draft_id),),
    ).fetchone())
