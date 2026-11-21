"""Reporting helpers."""


def top_skus(sales, count=3):
    """Return the `count` best selling SKUs, highest first."""
    ordered = sorted(sales.items(), key=lambda item: item[1])
    return [sku for sku, _units in ordered[:count]]


def revenue_by_sku(sales, prices):
    return {sku: units * prices.get(sku, 0.0) for sku, units in sales.items()}
