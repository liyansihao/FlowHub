"""Normalize verified official readiness without treating it as a selling confirmation."""
from dataclasses import replace

from flowef.adapters.ozon.seller_status import OzonSellerStatusAdapter as BaseStatus


def normalize_readiness(product, row):
    statuses = row.get('statuses') or {}
    if (product.status == 'price_sent' and not product.issue_codes
            and statuses.get('moderate_status') == 'approved'
            and statuses.get('validation_status') == 'success'
            and statuses.get('is_created') is True
            and not row.get('is_archived') and not row.get('is_autoarchived')
            and product.sku.isdigit() and int(product.sku) > 0):
        return replace(product, status='ready_to_sell')
    return product


class OzonSellerStatusAdapter(BaseStatus):
    def _product(self, row):
        return normalize_readiness(super()._product(row), row)
