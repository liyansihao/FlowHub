"""Short-lived read batches for the ERP follow lane only.

Reuse exact SKU observations through preparation and submission, as the old
runner reused its favorite rows. Writes invalidate that SKU before dispatch;
stock observations are never cached. Original journals still own all writes.
"""
import asyncio
from collections import OrderedDict

from .pipeline_modules.transport import StepTransport


class FollowBatchTransport(StepTransport):
    batches = OrderedDict()

    def __init__(self, transport, *, sku, namespace, circuit_namespace="local"):
        super().__init__(transport, ttl=60, namespace=namespace,
                         circuit_namespace=circuit_namespace)
        self.sku = str(sku)
        key = (asyncio.get_running_loop(), namespace, self.sku)
        self.cache = self.batches.setdefault(key, {})
        self.batches.move_to_end(key)
        while len(self.batches) > 256:
            self.batches.popitem(last=False)

    def generation_scope(self, path):
        # A write for SKU A must not discard SKU B's current read batch.
        # Shop identity remains shared by account, using the existing 60s TTL.
        if path in ('/api.product.favorite/lists', '/api.product.import_logs/index'):
            return (asyncio.get_running_loop(), (self.namespace, self.sku), path)
        return super().generation_scope(path)

    def invalidate(self, path):
        if path == '/authentication-failure':
            for (loop, account, sku), cache in self.batches.items():
                if loop is asyncio.get_running_loop() and account == self.namespace:
                    cache.clear()
                    for endpoint in ('/api.product.favorite/lists', '/api.product.import_logs/index'):
                        scope=(loop,(account,sku),endpoint)
                        self.generations[scope]=self.generations.get(scope,0)+1
        super().invalidate(path)


async def submit_prepared(service, journal, offer, *, native_follow, before):
    """Consume a confirmed favorite now; leave platform/stock work to readback."""
    await service.advance(offer)
    if native_follow and before == 'prepared' and journal.read(offer)['phase'] == 'ready':
        # The original CAS and preflight still run, including unknown-write handling.
        await service.advance(offer)
