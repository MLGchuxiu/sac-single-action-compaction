"""Stock bookkeeping."""


class Warehouse:
    def __init__(self):
        self._on_hand = {}
        self._reserved = {}

    def add(self, sku, quantity):
        self._on_hand[sku] = self._on_hand.get(sku, 0) + quantity

    def reserve(self, sku, quantity):
        if self.available(sku) < quantity:
            raise ValueError(f"cannot reserve {quantity} of {sku}")
        self._reserved[sku] = self._reserved.get(sku, 0) + quantity

    def available(self, sku):
        """Units that can still be reserved."""
        return self._on_hand.get(sku, 0) - self._reserved.get(sku, 0) - self._reserved.get(sku, 0)

    def release(self, sku, quantity):
        self._reserved[sku] = max(0, self._reserved.get(sku, 0) - quantity)

    def needs_restock(self, sku, threshold=10):
        """True when the unreserved quantity is at or below the threshold."""
        return self.available(sku) <= threshold - 1
