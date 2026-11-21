"""Task suite: five workloads that exercise the four required categories.

* ``code_edit``       — multi-turn code editing in a small repository (3 coupled bugs).
* ``doc_read``        — selective reading of a 6-document, ~300 KB corpus.
* ``multimodal``      — three images that must be read and combined.
* ``long_output``     — a long structured document that exceeds one reply.
* ``state_retention`` — an append-only draw ledger that cannot be re-read (the
  paper's §3.1 "coin toss" pathology, made concrete).
"""
from __future__ import annotations

import json
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw


@dataclass
class Task:
    id: str
    prompt: str
    input_details: str
    workdir: Path
    setup: object
    verify: object
    category: str = ""
    max_actions: int = 32


# --------------------------------------------------------------------------- #
# 1. code editing
# --------------------------------------------------------------------------- #

_CODE_FILES = {
    "billing/__init__.py": '"""Tiny billing package used by the SAC harness."""\n',
    "billing/rounding.py": '''"""Currency rounding helpers."""


def to_cents(amount):
    """Convert a float amount to integer cents."""
    return int(amount * 100)


def round_money(amount):
    """Round a float amount to two decimal places."""
    return round(amount, 1)
''',
    "billing/ledger.py": '''"""Ledger aggregation."""

from .rounding import round_money


def subtotal(entries):
    """Sum the amounts of the given entries."""
    total = 0.0
    for entry in entries[:-1]:
        total += entry["amount"]
    return total


def invoice_total(entries, tax_rate):
    """Return (subtotal, tax, total) for the entries."""
    base = subtotal(entries)
    tax = round_money(base * tax_rate)
    return round_money(base), tax, round_money(base + tax)
''',
    "billing/cli.py": '''"""Command line entry point."""

import json
import sys

from .ledger import invoice_total
from .rounding import to_cents


def main(argv):
    entries = json.loads(argv[1])
    tax_rate = float(argv[2])
    base, tax, total = invoice_total(entries, tax_rate)
    print(f"subtotal={base:.2f} tax={tax:.2f} total={total:.2f} cents={to_cents(total)}")


if __name__ == "__main__":
    main(sys.argv)
''',
    "test_billing.py": '''"""Regression tests for the billing package. Run: python3 test_billing.py"""

from billing.ledger import invoice_total, subtotal
from billing.rounding import round_money, to_cents

ENTRIES = [{"amount": 10.005}, {"amount": 20.015}, {"amount": 5.5}]


def test_rounding():
    assert round_money(10.005) == 10.01, f"round_money(10.005) -> {round_money(10.005)}"


def test_subtotal_includes_every_entry():
    assert subtotal(ENTRIES) == 35.52, f"subtotal -> {subtotal(ENTRIES)}"


def test_invoice_total():
    base, tax, total = invoice_total(ENTRIES, 0.2)
    assert (base, tax, total) == (35.52, 7.1, 42.62), f"invoice_total -> {(base, tax, total)}"


def test_cents():
    assert to_cents(42.62) == 4262, f"to_cents -> {to_cents(42.62)}"


if __name__ == "__main__":
    failures = 0
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        try:
            function()
            print(f"PASS {name}")
        except AssertionError as error:
            failures += 1
            print(f"FAIL {name}: {error}")
    print(f"{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
''',
}


def _setup_code(root: Path) -> None:
    for relative, content in _CODE_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _verify_code(root: Path, answer: str):
    completed = subprocess.run(["python3", "test_billing.py"], cwd=root,
                               capture_output=True, text=True, timeout=120)
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode == 0 and "0 failure(s)" in output:
        return True, 1.0, "all 4 regression tests pass"
    passed = output.count("PASS ")
    return False, passed / 4.0, output[-800:]


# --------------------------------------------------------------------------- #
# 2. long-document reading
# --------------------------------------------------------------------------- #

_DOC_FACTS = {
    "doc01.txt": ("Vendor onboarding policy", "onboarding fee", 1375),
    "doc02.txt": ("Data retention policy", "retention window in days", 428),
    "doc03.txt": ("Service credit schedule", "monthly service credit", 2110),
    "doc04.txt": ("Helios amendment", "Helios penalty percentage", 27),
    "doc05.txt": ("Audit rights", "audit notice days", 63),
    "doc06.txt": ("Disaster recovery appendix", "recovery point objective minutes", 341),
}


def _generate_docs(root: Path) -> None:
    random.seed(20261114)
    topics = [
        "the supplier shall maintain records", "each party shall notify the other",
        "the agreement remains in force", "the processor may subcontract",
        "the parties shall cooperate in good faith", "the service level applies",
        "the audit shall be conducted during business hours", "the fee is payable quarterly",
        "the customer bears the cost", "the vendor shall remediate",
    ]
    documents = root / "documents"
    documents.mkdir(parents=True, exist_ok=True)
    for name, (title, fact_label, fact_value) in _DOC_FACTS.items():
        paragraphs = []
        for index in range(90):
            sentence = random.choice(topics)
            filler = " ".join(random.choice([
                "the parties agree", "the schedule applies", "this clause survives termination",
                "notice must be in writing", "the obligations are cumulative", "the appendix prevails",
            ]) for _ in range(6))
            paragraphs.append(f"{index + 1}. Section {index + 1}.{random.randint(1, 9)} — "
                              f"{sentence.capitalize()}; {filler}. "
                              f"Reference {random.randint(1000, 9999)}-{random.randint(10, 99)} applies to this clause.")
        insertion = random.randint(30, 60)
        paragraphs.insert(insertion,
                          f"{insertion + 1}. {fact_label.capitalize()} — {title}: the agreed value is "
                          f"{fact_value} ({fact_label}). This figure is authoritative for the current term.")
        (documents / name).write_text(
            f"{title}\n" + "=" * len(title) + "\n\n" + "\n\n".join(paragraphs) + "\n",
            encoding="utf-8",
        )


def _setup_docs(root: Path) -> None:
    _generate_docs(root)


def _verify_docs(root: Path, answer: str):
    expected = 1375 + 428 + 27 + 341
    numbers = {int(value) for value in __import__("re").findall(r"\d+", answer.replace(",", ""))}
    if expected in numbers:
        return True, 1.0, f"correct total {expected}"
    for value in (1375, 428, 27, 341):
        if value in numbers:
            return False, 0.25, f"found partial value {value}, expected {expected}"
    return False, 0.0, f"expected {expected}; answer had {sorted(numbers)[:12]}"


# --------------------------------------------------------------------------- #
# 3. multimodal
# --------------------------------------------------------------------------- #

def _setup_images(root: Path) -> None:
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    chart = Image.new("RGB", (520, 320), "white")
    draw = ImageDraw.Draw(chart)
    draw.text((20, 10), "Quarterly units — Widget Line", fill="black")
    values = [("Q1", 120), ("Q2", 180), ("Q3", 275), ("Q4", 90)]
    for index, (label, value) in enumerate(values):
        x = 60 + index * 110
        height = int(value * 0.7)
        draw.rectangle([x, 280 - height, x + 60, 280], outline="black", fill="lightblue")
        draw.text((x, 288), f"{label}={value}", fill="black")
    draw.line([40, 280, 500, 280], fill="black")
    chart.save(assets / "chart.png")

    table = Image.new("RGB", (560, 260), "white")
    draw = ImageDraw.Draw(table)
    draw.text((20, 10), "Parts catalogue", fill="black")
    rows = [("Item", "SKU", "Unit price"), ("Gasket", "GX-114", "48"),
            ("Bearing", "BR-207", "96"), ("Seal", "SL-330", "17")]
    for row, values in enumerate(rows):
        draw.text((30, 50 + row * 36), f"{values[0]:<10}{values[1]:<10}{values[2]:>6}", fill="black")
    table.save(assets / "table.png")

    receipt = Image.new("RGB", (420, 300), "white")
    draw = ImageDraw.Draw(receipt)
    lines = ["NORTHWIND SUPPLY", "Receipt #4412", "Gasket   x2   48.00",
             "Bearing  x1   96.00", "Shipping      11.00", "TOTAL        203.00"]
    for index, line in enumerate(lines):
        draw.text((24, 20 + index * 38), line, fill="black")
    receipt.save(assets / "receipt.png")


def _verify_images(root: Path, answer: str):
    expected = 275 + 48 + 203
    numbers = {int(value) for value in __import__("re").findall(r"\d+", answer.replace(",", ""))}
    if expected in numbers:
        return True, 1.0, f"correct total {expected}"
    if 275 in numbers:
        return False, 0.34, f"read chart only; expected {expected}"
    return False, 0.0, f"expected {expected}"


# --------------------------------------------------------------------------- #
# 4. long-form output
# --------------------------------------------------------------------------- #

_LONG_SECTIONS = [
    "Context and goals", "System overview", "Data model", "Ingestion pipeline",
    "Compaction policy", "Failure handling", "Observability", "Rollout plan",
]


def _setup_long(root: Path) -> None:
    (root / "docs").mkdir(parents=True, exist_ok=True)


def _verify_long(root: Path, answer: str):
    target = root / "docs" / "design.md"
    if not target.exists():
        return False, 0.0, "docs/design.md was not written"
    text = target.read_text(encoding="utf-8")
    words = len(text.split())
    headings = sum(1 for section in _LONG_SECTIONS if section.lower() in text.lower())
    has_json = False
    detail = ""
    marker = text.find("```json")
    if marker >= 0:
        end = text.find("```", marker + 7)
        try:
            payload = json.loads(text[marker + 7:end])
            has_json = {"version", "components", "budgets"}.issubset(payload)
            detail = f"json keys={sorted(payload)}"
        except (json.JSONDecodeError, ValueError) as error:
            detail = f"json invalid: {error}"
    score = 0.0
    score += 0.35 if words >= 3500 else 0.2 if words >= 1800 else 0.0
    score += 0.4 * (headings / len(_LONG_SECTIONS))
    score += 0.25 if has_json else 0.0
    ok = words >= 3500 and headings == len(_LONG_SECTIONS) and has_json
    return ok, min(1.0, score), f"words={words} headings={headings}/8 json={has_json} {detail}"


# --------------------------------------------------------------------------- #
# 5. state retention (append-only draws)
# --------------------------------------------------------------------------- #

_DRAW_CODES = [4017, 8830, 1265, 9042, 3378, 6619, 2480, 7726]

_DRAW_SCRIPT = '''#!/usr/bin/env python3
"""Append-only draw ledger. Each invocation prints exactly one new code.

Codes already drawn are never reprinted: the script keeps only a counter and
deletes the previous output, so a draw exists only in the transcript that
observed it.
"""
import json
import pathlib
import sys

CODES = {codes}
state_path = pathlib.Path("draws.json")

state = json.loads(state_path.read_text()) if state_path.exists() else {{"drawn": 0}}
drawn = state["drawn"]
if drawn >= len(CODES):
    print("no draws left")
    sys.exit(0)
code = CODES[drawn]
state["drawn"] = drawn + 1
state_path.write_text(json.dumps(state))
print(f"draw #{{drawn + 1}}: code {{code}}")
'''


def _setup_draws(root: Path) -> None:
    (root / "draw.py").write_text(_DRAW_SCRIPT.format(codes=_DRAW_CODES), encoding="utf-8")


def _verify_draws(root: Path, answer: str):
    expected_total = sum(_DRAW_CODES)
    expected_first = _DRAW_CODES[0]
    import re
    numbers = [int(value) for value in re.findall(r"\d+", answer.replace(",", ""))]
    has_total = expected_total in numbers
    has_first = expected_first in numbers
    if has_total and has_first:
        return True, 1.0, f"total {expected_total} and first code {expected_first} both correct"
    if has_total or has_first:
        return False, 0.5, f"partial: total={has_total} first={has_first}"
    return False, 0.0, f"expected total {expected_total} and first code {expected_first}"




# --------------------------------------------------------------------------- #
# 6. long-horizon multi-file debugging (8 coupled defects)
# --------------------------------------------------------------------------- #

_SWE_SEED = Path(__file__).parent.parent / "assets" / "swe_seed"


def _setup_swe(root: Path) -> None:
    import shutil as _shutil
    _shutil.copytree(_SWE_SEED, root, dirs_exist_ok=True)


def _verify_swe(root: Path, answer: str):
    completed = subprocess.run(
        ["python3", "-m", "unittest", "discover", "-s", "tests"],
        cwd=root, capture_output=True, text=True, timeout=180,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode == 0:
        return True, 1.0, "all 8 regression tests pass"
    failures = output.count("FAIL:")
    if failures == 0:
        # The suite could not even import: the workspace is broken, not partially fixed.
        return False, 0.0, "test suite failed to import: " + output[-500:]
    return False, (8 - failures) / 8, output[-700:]


# --------------------------------------------------------------------------- #
# 7. multi-question document QA (6 lookups in 8 documents)
# --------------------------------------------------------------------------- #

_QA_FACTS = [
    ("doc01.txt", "SLA credit cap", 3840),
    ("doc02.txt", "security deposit", 12500),
    ("doc03.txt", "renewal notice days", 91),
    ("doc04.txt", "data residency region code", 617),
    ("doc05.txt", "maximum subcontractors", 14),
    ("doc06.txt", "escalation response hours", 27),
]


def _setup_qa(root: Path) -> None:
    random.seed(7714)
    documents = root / "documents"
    documents.mkdir(parents=True, exist_ok=True)
    filler = [
        "the parties acknowledge the schedule", "this section survives termination",
        "notice must be delivered in writing", "the appendix prevails over the body",
        "the supplier bears its own costs", "the audit window is agreed separately",
    ]
    for name, label, value in _QA_FACTS:
        paragraphs = []
        for index in range(70):
            paragraphs.append(
                f"{index + 1}. Clause {index + 1}.{random.randint(1, 9)} — "
                + "; ".join(random.choice(filler) for _ in range(5))
                + f". Reference {random.randint(1000, 9999)}-{random.randint(10, 99)}."
            )
        position = random.randint(25, 45)
        paragraphs.insert(position,
                          f"{position + 1}. {label.capitalize()} — the agreed value is {value}. "
                          f"This figure controls the {name} schedule.")
        (documents / name).write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    # Two decoy documents with plausible but wrong numbers.
    for name in ("doc07.txt", "doc08.txt"):
        body = "\n\n".join(
            f"{index + 1}. Clause {index + 1} — " + "; ".join(random.choice(filler) for _ in range(5)) + "."
            for index in range(60)
        )
        (documents / name).write_text(body + "\n", encoding="utf-8")


def _verify_qa(root: Path, answer: str):
    import re as _re
    payload = None
    for match in _re.finditer(r"\{[^{}]*\}", answer):
        try:
            candidate = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if any(str(key).lower().startswith("q") for key in candidate):
            payload = candidate
            break
    if payload is None:
        return False, 0.0, "no JSON answer object found"
    values = []
    for index, (_, _, expected) in enumerate(_QA_FACTS, 1):
        found = None
        for key, value in payload.items():
            if str(key).lower().strip() in (f"q{index}", f"question{index}", str(index)):
                found = value
        try:
            found = int(str(found).strip())
        except (TypeError, ValueError):
            found = None
        values.append(found == expected)
    return all(values), sum(values) / len(values), f"per-question: {values}"


# --------------------------------------------------------------------------- #
# 8. million-token monorepo debugging
# --------------------------------------------------------------------------- #

def _setup_million(root: Path) -> None:
    from .repo_gen import generate
    generate(root)


def _verify_million(root: Path, answer: str):
    completed = subprocess.run(
        ["python3", "-m", "unittest", "discover", "-s", "tests"],
        cwd=root, capture_output=True, text=True, timeout=180,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode == 0:
        return True, 1.0, "settlement tests pass"
    failures = output.count("FAIL:") + output.count("ERROR:")
    return False, max(0.0, 1.0 - failures / 2), output[-500:]


    return False, 0.0, f"count={count} (expected 6), names={len(hits)}/5"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

TASKS: dict[str, Task] = {
    "code_edit": Task(
        id="code_edit",
        category="code",
        prompt=(
            "The `billing` package in this workspace has three defects that make `test_billing.py` fail: "
            "money rounding is wrong, the subtotal silently drops the last ledger entry, and the invoice total "
            "does not add up. Read the code, fix every defect, and run `python3 test_billing.py` until all four "
            "tests pass. Do not weaken the tests. Report which files you changed and the final test output."
        ),
        input_details="Workspace files: billing/__init__.py, billing/rounding.py, billing/ledger.py, billing/cli.py, test_billing.py",
        workdir=Path("runs/code_edit"),
        setup=_setup_code,
        verify=_verify_code,
    ),
    "doc_read": Task(
        id="doc_read",
        category="documents",
        prompt=(
            "Using only the documents in `documents/`, compute the following total and answer with the number: "
            "the onboarding fee in doc01, plus the retention window in days from doc02, plus the Helios penalty "
            "percentage in doc04, plus the recovery point objective minutes in doc06. "
            "Answer with the exact integer total and list the four values you used."
        ),
        input_details=(
            "Available documents: documents/doc01.txt, doc02.txt, doc03.txt, doc04.txt, doc05.txt, doc06.txt "
            "(each roughly 40-60 KB). Read slices with read_file offset/limit or grep; do not dump whole files."
        ),
        workdir=Path("runs/doc_read"),
        setup=_setup_docs,
        verify=_verify_docs,
        max_actions=30,
    ),
    "multimodal": Task(
        id="multimodal",
        category="multimodal",
        prompt=(
            "Three images are in the `assets` folder of the workspace: chart.png, table.png and receipt.png. "
            "View all three, then compute: the Q3 value from the chart, plus the unit price of the Gasket in the "
            "table, plus the receipt TOTAL. Answer with the exact integer total and the three values."
        ),
        input_details="Images: assets/chart.png, assets/table.png, assets/receipt.png. Use view_image to see them.",
        workdir=Path("runs/multimodal"),
        setup=_setup_images,
        verify=_verify_images,
    ),
    "long_output": Task(
        id="long_output",
        category="long-output",
        prompt=(
            "Write a technical design document to `docs/design.md`. It must be at least 3500 words and contain "
            "all eight sections: " + ", ".join(f"{index + 1}. {title}" for index, title in enumerate(_LONG_SECTIONS)) +
            ". End with a ```json fenced appendix that parses as JSON and has the keys version, components and budgets. "
            "Work in chunks: create the file, then append or edit sections until complete."
        ),
        input_details="Output path: docs/design.md (create it). No source material is provided.",
        workdir=Path("runs/long_output"),
        setup=_setup_long,
        verify=_verify_long,
        max_actions=44,
    ),
    "swe_multifile": Task(
        id="swe_multifile",
        category="code",
        prompt=(
            "The `inventory` package has eight independent defects; `python3 -m unittest discover -s tests -v` "
            "reports eight failures, one per defect. Fix the package so all eight tests pass. "
            "Do not modify the tests. The defects live in pricing.py, stock.py, orders.py, reports.py and cli.py; "
            "several need more than a mechanical edit (think about ordering, rounding and threshold semantics). "
            "Report the final test output."
        ),
        input_details="Workspace: inventory/{__init__,pricing,stock,orders,reports,cli}.py and tests/test_all.py",
        workdir=Path("runs/swe_multifile"),
        setup=_setup_swe,
        verify=_verify_swe,
        max_actions=56,
    ),
    "doc_qa_multi": Task(
        id="doc_qa_multi",
        category="documents",
        prompt=(
            "Eight documents are in `documents/`. Answer six independent questions by looking up the relevant "
            "clause in each document: (q1) the SLA credit cap, (q2) the security deposit, (q3) the renewal notice "
            "in days, (q4) the data residency region code, (q5) the maximum number of subcontractors, "
            "(q6) the escalation response in hours. Some documents are decoys. "
            "Answer with a single JSON object of the form {\"q1\": 0, ..., \"q6\": 0} and nothing else."
        ),
        input_details="Documents: documents/doc01.txt .. doc08.txt (each ~40 KB). Use grep + read_file offset/limit.",
        workdir=Path("runs/doc_qa_multi"),
        setup=_setup_qa,
        verify=_verify_qa,
        max_actions=32,
    ),
    "million_repo": Task(
        id="million_repo",
        category="code",
        prompt=(
            "This workspace is a ~1M-token Python settlement monorepo (about 1,300 files under atlas/ and docs/). "
            "`python3 -m unittest discover -s tests -v` fails. Find the single defective module, fix it, and make "
            "both settlement tests pass. Do not read whole directories into context: locate the symbol with grep, "
            "then read only the slice you need. Report the file you changed and the final test output."
        ),
        input_details=(
            "Repo layout: atlas/core/ (10 modules), atlas/services/ (950 modules), atlas/vendor/ (130), "
            "docs/ (190 runbooks), tests/test_settlement.py. Total ~3.5 MB."
        ),
        workdir=Path("runs/million_repo"),
        setup=_setup_million,
        verify=_verify_million,
        max_actions=40,
    ),
    "state_retention": Task(
        id="state_retention",
        category="state",
        prompt=(
            "Run `python3 draw.py` exactly 8 times. Each run prints one draw with a 4-digit code. "
            "The script never reprints an old draw, so the codes exist only in the outputs you observed. "
            "When all 8 draws are done, answer with the sum of all eight codes and the code of draw #1."
        ),
        input_details="Tool: python3 draw.py (append-only; one new code per call).",
        workdir=Path("runs/state_retention"),
        setup=_setup_draws,
        verify=_verify_draws,
        max_actions=32,
    ),
}