"""Which simulations count, shared by the report tools.

A simulation is *included* when

* its termination reason is not ``infrastructure_error`` (endpoint outage, exhausted quota), and
* none of its DSH turns was cut off with an endpoint error (HTTP 429 rate limit): the agent then
  answers with a fallback sentence, which is an infrastructure artifact, not an outcome of the arm.

Turns that ended with ``max-tokens`` are kept: the model spending its whole output budget on one
step is model behaviour, the same in every arm.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIMS = ROOT / "benchmarks" / "tau2-bench" / "data" / "simulations"
DSH_HOME = ROOT / "work" / "dshhome"
DOMAINS = ("airline", "retail", "telecom")


def _reason(sim) -> str:
    return str(getattr(sim.termination_reason, "value", sim.termination_reason))


def turns_of(sim) -> list[dict]:
    """The per-turn records the agent attached to its replies (empty for the official `llm_agent`)."""
    records, seen = [], set()
    for message in sim.messages:
        raw = getattr(message, "raw_data", None) or {}
        if isinstance(raw, dict) and isinstance(raw.get("dsh"), dict):
            record = raw["dsh"]
            key = (record.get("session_id"), record.get("turn"))
            if key not in seen:
                seen.add(key)
                records.append(record)
    return sorted(records, key=lambda record: (str(record.get("session_id")), record.get("turn") or 0))


def session_id_of(sim) -> str | None:
    for message in sim.messages:
        raw = getattr(message, "raw_data", None) or {}
        if isinstance(raw, dict) and isinstance(raw.get("dsh"), dict):
            return raw["dsh"].get("session_id")
    return None


def is_infra(sim) -> bool:
    return _reason(sim) == "infrastructure_error"


def is_cut_off(sim) -> bool:
    return any(turn.get("exit_code") not in (0, None) and (turn.get("reason") or {}).get("kind") == "error" for turn in turns_of(sim))


def included(sim) -> bool:
    return not is_infra(sim) and not is_cut_off(sim)


def load_run(tag: str, arm: str, domain: str):
    from tau2.data_model.simulation import Results

    path = SIMS / f"{tag}_{arm}_{domain}" / "results.json"
    return Results.load(path).simulations if path.exists() else None


def included_sims(tag: str, arms, domains=DOMAINS) -> dict:
    """`{arm: [(domain, sim), ...]}` for the included simulations; also caches every loaded run."""
    cache, out = {}, {}
    for arm in arms:
        out[arm] = []
        for domain in domains:
            sims = load_run(tag, arm, domain)
            cache[(arm, domain)] = sims
            if sims:
                out[arm] += [(domain, sim) for sim in sims if included(sim)]
    return out, cache
