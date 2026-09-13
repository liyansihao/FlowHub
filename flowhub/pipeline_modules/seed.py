"""Seed discovery and exact-store page ingestion, without ranking-list fallback."""
from ..source_acquisition import SourceAcquirer
from ..source_library import SourceLibrary
from ..storefront import StorefrontCollector
from ..plugin_comparebot import candidate


class SeedModule:
    name = 'seed'

    def __init__(self, db):
        self.db = db
        self.library = SourceLibrary(db)
        self.acquirer = SourceAcquirer(self.library)
        self.storefront = StorefrontCollector(self.library)

    async def run(self, owner, token):
        # Existing direct ERP order/shop endpoints supply root seeds. Never
        # silently substitute ranking/category lists for whole-store products.
        return await self.acquirer.cycle(owner, token, kinds=('own_orders', 'own_shop'))

    def prepare_stores(self, owner, manifest):
        return self.storefront.prepare(owner, manifest)

    def ingest_packet(self, owner, seller, html, requested_url, artifact):
        # The existing parser binds exact seller, page cursor and source roots;
        # packet and checkpoint commit together, including duplicate pages.
        return self.storefront.ingest(owner, seller, html, requested_url, artifact)

    def ready_for_review(self, product, now=None):
        # A typed boundary: missing measurement facts remain in seed repair.
        return candidate(product, now)
