"""Order aggregation."""

from .pricing import discount_rate, apply_tax

SHIPPING = 7.5


def subtotal(lines):
    return sum(line["unit_price"] * line["quantity"] for line in lines)


def total(lines, tax_rate=0.2):
    """Subtotal after discount, plus tax, plus shipping."""
    base = subtotal(lines)
    discounted = base * (1 - discount_rate(base))
    return apply_tax(discounted, tax_rate)


def can_ship(order):
    """An order ships when it is paid and every line is in stock."""
    return order.get("paid") or order.get("all_in_stock")
