"""Explicit account-bound VAT values verified against this account's ERP-created products."""

import json
import time
from decimal import Decimal, InvalidOperation

from .modules import ModuleError


def verified_sample_profile(client_id, offers, items):
    """Match the user's existing ERP settings only with exact same-account samples."""
    if len(set(offers)) < 3 or not isinstance(items, list):
        raise ValueError('vat_samples_insufficient')
    if len(items) != len(offers) or {i.get('offer_id') for i in items} != set(offers):
        raise ValueError('vat_sample_identity_mismatch')
    values=set()
    for item in items:
        price=item.get('price') or {}
        value=price.get('vat')
        if price.get('currency_code') != 'CNY' or isinstance(value, bool) or value is None:
            raise ValueError('vat_sample_invalid')
        try:
            rate=Decimal(str(value))
        except InvalidOperation:
            raise ValueError('vat_sample_invalid') from None
        if not rate.is_finite() or not 0 <= rate <= 1:
            raise ValueError('vat_sample_invalid')
        values.add(rate)
    if len(values) != 1:
        raise ValueError('vat_samples_disagree')
    return {'client_id':str(client_id),'vat':str(next(iter(values))),
            'source':'Ozon /v5/product/info/prices: same-account ERP-created stock-verified products; matching existing ERP settings',
            'verified_at':time.time(),'offers':offers}


def configured_vat(database, client_id):
    path = database.directory / "store-vat.json"
    if not path.exists():
        return None
    profiles = json.loads(path.read_text())
    profile = profiles.get(str(client_id))
    if profile is None:
        return None
    if str(profile.get("client_id")) != str(client_id):
        raise ModuleError("VAT profile account mismatch")
    value = profile.get("vat")
    try:
        rate = Decimal(value) if isinstance(value, str) else Decimal("NaN")
    except InvalidOperation:
        raise ModuleError("invalid configured VAT") from None
    if not rate.is_finite() or not 0 <= rate <= 1 or not profile.get("source"):
        raise ModuleError("invalid configured VAT")
    return profile
