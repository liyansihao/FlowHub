"""A scheduler deferral proves inventory was not dispatched."""

from flowef.adapters.ozon.seller_inventory import OzonSellerInventoryAdapter
from flowef.application.errors import RequestNotSent

from .official_api import OfficialDeferred


class ScheduledInventoryAdapter(OzonSellerInventoryAdapter):
    def _product(self, row):
        from .official_status import normalize_readiness
        return normalize_readiness(super()._product(row), row)

    async def set_stocks(self, product, warehouse_ids, stock):
        try:
            return await super().set_stocks(product, warehouse_ids, stock)
        except OfficialDeferred as error:
            raise RequestNotSent("official inventory deferred before dispatch") from error


async def exact_erp_product(base, observed):
    """Official product IDs must never be used as ERP mutation record IDs."""
    current = await base.find_product(observed.shop_id, observed.offer_id)
    fields = ("shop_id", "offer_id", "product_id", "sku")
    if current is None or any(getattr(current, k) != getattr(observed, k) for k in fields):
        raise ValueError("erp_mutation_identity_unconfirmed")
    return current
