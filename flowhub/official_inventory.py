"""A scheduler deferral proves inventory was not dispatched."""

from flowef.adapters.ozon.seller_inventory import OzonSellerInventoryAdapter
from flowef.application.errors import ExternalContractError, RateLimited, RequestNotSent

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
        except ExternalContractError as error:
            if (str(error) == "Seller inventory acknowledgement rejected or mismatched"
                    and explicit_stock_rate_limit(self.journal.read(product.offer_id), product,
                                                  self.warehouse_id, stock)):
                raise RateLimited(60) from error
            raise


def explicit_stock_rate_limit(record, product, warehouse_id, stock):
    """Recognize only the exact, journaled rejection of this inventory write."""
    if record['phase'] != 'stock_pending':
        return False
    details = record['details']
    request = details.get('stock_request') or {}
    response = details.get('stock_response') or {}
    sent = request.get('stocks')
    rows = response.get('result') if isinstance(response, dict) else None
    if (details.get('stock_write_transport') != 'ozon-seller-api'
            or details.get('stock_http_status') != 200
            or not isinstance(sent, list) or len(sent) != 1
            or not isinstance(rows, list) or len(rows) != 1):
        return False
    sent, row = sent[0], rows[0]
    if not isinstance(sent, dict) or not isinstance(row, dict):
        return False
    errors = row.get('errors')
    return (sent.get('offer_id') == product.offer_id
            and str(sent.get('product_id')) == str(product.product_id)
            and str(sent.get('warehouse_id')) == str(warehouse_id)
            and sent.get('stock') == stock
            and row.get('offer_id') == product.offer_id
            and str(row.get('warehouse_id')) == str(warehouse_id)
            and str(row.get('product_id')) in ('0', str(product.product_id))
            and row.get('updated') is False
            and isinstance(errors, list) and bool(errors)
            and all(isinstance(item, dict) and item.get('code') == 'TOO_MANY_REQUESTS'
                    for item in errors))


async def exact_erp_product(base, observed):
    """Official product IDs must never be used as ERP mutation record IDs."""
    current = await base.find_product(observed.shop_id, observed.offer_id)
    fields = ("shop_id", "offer_id", "product_id", "sku")
    if current is None or any(getattr(current, k) != getattr(observed, k) for k in fields):
        raise ValueError("erp_mutation_identity_unconfirmed")
    return current
