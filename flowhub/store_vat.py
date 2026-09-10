"""Explicit account-bound VAT values verified against this account's ERP-created products."""

import json
from decimal import Decimal, InvalidOperation

from .modules import ModuleError


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
