"""Pricing rules."""

TIERS = [("bronze", 0, 0.0), ("silver", 1000, 0.05), ("gold", 5000, 0.10)]


def tier_for(amount):
    """Return the tier name for a spend amount (thresholds are inclusive)."""
    name = "bronze"
    for tier, threshold, _rate in TIERS:
        if amount > threshold:
            name = tier
    return name


def discount_rate(amount):
    """Return the discount rate for a spend amount."""
    for tier, threshold, rate in TIERS:
        if amount > threshold:
            return rate
    return 0.0


def apply_tax(amount, rate):
    """Return the taxed amount, rounded to cents."""
    return amount * (1 + rate)
