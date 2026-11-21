"""Generate a ~1M-token Python monorepo with one deep, findable bug.

The repo is deliberately realistic in shape: a large `services/` layer where each
module is boilerplate-heavy, a smaller `core/`, vendored helpers, docs, and a
test suite. Exactly one module is wrong, and the failing test names the symbol,
so the task is solvable by selective reading (grep + narrow reads) without ever
ingesting the whole tree — the paper's RQ2 setup at a million-token scale.

The generator is deterministic: same seed, same bytes, same bug.
"""
from __future__ import annotations

import random
from pathlib import Path

SEED = 20261114
TOTAL_SERVICES = 950
TOTAL_CORE = 10
TOTAL_VENDOR = 130
TOTAL_DOCS = 190
BUG_SERVICE = 173


def _service_module(index: int, rng: random.Random) -> str:
    """One service module: ~9 KB of plausible settlement-domain code."""
    name = f"svc_{index:04d}"
    entity = rng.choice(["invoice", "settlement", "ledger", "payout", "refund", "chargeback", "reconciliation"])
    currency = rng.choice(["USD", "EUR", "CNY", "JPY", "GBP"])
    rate = round(rng.uniform(0.5, 2.0), 4)
    functions = []
    for slot in range(6):
        functions.append(f'''
def compute_{entity}_{slot}(amount: float, quantity: int = {slot + 1}) -> float:
    """Compute the {entity} amount for slot {slot}."""
    if amount < 0:
        raise ValueError("amount must be non-negative")
    base = amount * quantity
    adjusted = base * {rate}
    return round(adjusted, 2)
''')
    bug = ""
    if index == BUG_SERVICE:
        bug = '''

def convert_units(amount: float, factor: float) -> float:
    """Convert an amount between units.

    Settlement amounts are stored in minor units (cents); this converts a value
    in major units to minor units using the supplied factor.
    """
    # BUG: converts minor->major instead of major->minor.
    return round(amount * factor / 100.0, 2)
'''
    header = f'''"""{entity.capitalize()} service {index:04d} ({currency}).

Auto-generated settlement service used by the atlas platform. Each service
exposes a handful of pure functions plus a small registry entry.
"""

from __future__ import annotations

from atlas.core.money import Money
from atlas.core.errors import SettlementError

SERVICE_NAME = "{name}"
CURRENCY = "{currency}"
DEFAULT_FACTOR = {rate}
'''
    registry = f'''

REGISTRY = {{
    "name": SERVICE_NAME,
    "currency": CURRENCY,
    "factor": DEFAULT_FACTOR,
    "handlers": [{", ".join(f"compute_{entity}_{slot}" for slot in range(6))}],
}}


def describe() -> dict:
    """Return a human-readable description of this service."""
    return {{"service": SERVICE_NAME, "currency": CURRENCY, "handlers": len(REGISTRY["handlers"])}}
'''
    return header + "".join(functions) + bug + registry


def _core_module(index: int, rng: random.Random) -> str:
    name = ["money", "errors", "time", "ids", "config", "logging", "retry", "cache", "metrics", "tracing"][index % 10]
    body = "\n".join(
        f'''
def helper_{index}_{slot}(value: float) -> float:
    """Helper {slot} for the {name} core module."""
    return round(value * {round(rng.uniform(0.9, 1.1), 3)}, 4)
'''
        for slot in range(8)
    )
    extra = ""
    if name == "money":
        extra = '''

class Money:
    """Minor-unit money value."""

    __slots__ = ("cents", "currency")

    def __init__(self, cents: int, currency: str = "USD"):
        self.cents = int(cents)
        self.currency = currency

    def __repr__(self) -> str:
        return f"Money({self.cents}, {self.currency!r})"
'''
    elif name == "errors":
        extra = '''

class SettlementError(RuntimeError):
    """Raised when a settlement cannot be computed."""
'''
    return f'"""Core module: {name}."""\n' + extra + "\n" + body


def _vendor_module(index: int, rng: random.Random) -> str:
    return f'''"""Vendored helper {index:03d}."""

CONSTANTS = {{
{chr(10).join(f'    "k{slot}": {rng.randint(1, 9999)},' for slot in range(20))}
}}


def vendor_call_{index}(value: float) -> float:
    """Vendored transform."""
    return value * CONSTANTS["k0"] / 1000.0
'''


def _doc(index: int, rng: random.Random) -> str:
    topic = rng.choice(["settlement", "reconciliation", "payout", "ledger", "audit", "fx"])
    lines = [f"# {topic.capitalize()} runbook {index:03d}", ""]
    for paragraph in range(14):
        lines.append(
            f"## {paragraph + 1}. {rng.choice(['Overview', 'Procedure', 'Checks', 'Rollback', 'Ownership'])}"
        )
        lines.append("")
        for _ in range(6):
            lines.append(
                f"- {rng.choice(['Verify', 'Record', 'Escalate', 'Retry', 'Reconcile'])} the "
                f"{topic} step with reference {rng.randint(100000, 999999)} before continuing."
            )
        lines.append("")
    return "\n".join(lines)


def generate(root: Path) -> dict:
    """Write the repository under ``root`` and return a manifest."""
    rng = random.Random(SEED)
    package = root / "atlas"
    (package / "services").mkdir(parents=True, exist_ok=True)
    (package / "core").mkdir(parents=True, exist_ok=True)
    (package / "vendor").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)

    (package / "__init__.py").write_text('"""Atlas settlement platform."""\n', encoding="utf-8")
    (package / "services" / "__init__.py").write_text("", encoding="utf-8")
    (package / "core" / "__init__.py").write_text("", encoding="utf-8")
    (package / "vendor" / "__init__.py").write_text("", encoding="utf-8")

    for index in range(TOTAL_CORE):
        name = ["money", "errors", "time", "ids", "config", "logging", "retry", "cache", "metrics", "tracing"][index % 10]
        (package / "core" / f"{name}.py").write_text(_core_module(index, rng), encoding="utf-8")
    for index in range(TOTAL_SERVICES):
        (package / "services" / f"svc_{index:04d}.py").write_text(_service_module(index, rng), encoding="utf-8")
    for index in range(TOTAL_VENDOR):
        (package / "vendor" / f"vendor_{index:03d}.py").write_text(_vendor_module(index, rng), encoding="utf-8")
    for index in range(TOTAL_DOCS):
        (root / "docs" / f"runbook_{index:03d}.md").write_text(_doc(index, rng), encoding="utf-8")

    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_settlement.py").write_text(
        '''"""Settlement regression tests."""

import unittest

from atlas.services.svc_0173 import convert_units


class SettlementTests(unittest.TestCase):
    def test_convert_units_to_minor_units(self):
        # 12.34 major units at factor 1.0 must become 1234 minor units.
        self.assertEqual(convert_units(12.34, 1.0), 1234.0)

    def test_convert_units_with_fx_factor(self):
        # 100.00 major units at an FX factor of 1.5 must become 15000 minor units.
        self.assertEqual(convert_units(100.0, 1.5), 15000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
''',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# Atlas settlement platform\n\n"
        "A large multi-service settlement monorepo. Run the test suite with:\n\n"
        "```\npython3 -m unittest discover -s tests -v\n```\n",
        encoding="utf-8",
    )

    files = [path for path in root.rglob("*") if path.is_file()]
    return {
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "bug_module": "atlas/services/svc_0173.py",
    }