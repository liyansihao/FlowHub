"""Continue an immutable Maozi intent, including uncertain-write reconciliation."""
from ..plugin_publication import advance


class PublicationModule:
    name = 'publication'

    async def run(self, db, owner, sku, seller):
        return await advance(db, owner, sku, seller)
