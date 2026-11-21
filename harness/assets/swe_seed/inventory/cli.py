"""Command line interface."""

import json
import sys

from .orders import total, subtotal
from .pricing import tier_for, discount_rate


def summarize(payload):
    lines = payload["lines"]
    tax_rate = payload.get("tax_rate", 0.2)
    return {
        "subtotal": round(subtotal(lines), 2),
        "total": round(total(lines, tax_rate), 2),
        "tier": tier_for(subtotal(lines)),
        "discount": discount_rate(subtotal(lines)),
    }


def main(argv):
    print(json.dumps(summarize(json.loads(argv[1])), sort_keys=True))


if __name__ == "__main__":
    main(sys.argv)
