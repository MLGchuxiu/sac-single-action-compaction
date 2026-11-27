"""The official ``tau2`` command line with the DSH agent registered.

Every official option works unchanged; ``--agent dsh_agent`` selects the DSH agent, whose settings travel in
``--agent-llm-args`` (JSON):

    work/tau2/venv/bin/python -m harness.tau2.run run \
        --domain airline --task-split-name base \
        --agent dsh_agent --agent-llm dsh/GLM-5.3-Inner \
        --agent-llm-args '{"profile": "tau2-sac"}' \
        --user-llm openai/gpt-4.1 --num-trials 1 --max-concurrency 1 --save-to sac_airline

``--agent-llm`` is only a label for the run name and metadata here; the agent's model is whatever the DSH
profile routes to.

Environment additions of this wrapper (the official CLI has neither):

``TAU2_JUDGE_LLM``        model string for the NL-assertion judge (retail's reward basis needs it). τ²
                          hard-codes ``gpt-4.1-2025-04-14``; this variable overrides that name in both the
                          config module and the evaluator that imported it.
``TAU2_DSH_HOME``         DSH home holding the ``tau2-*`` profiles (default: ``work/dshhome``).
provider keys             if ``<TAU2_DSH_HOME>/.credentials.yaml`` has a ``refs:`` block, missing
                          ``<NAME>=<value>`` entries are exported (e.g. ``JMS_PROXY_API_KEY``). Values are
                          never printed.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# tau2 logs its registry at import time; keep --help and the run output readable.
from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="WARNING")

from harness.tau2.dsh_agent import AGENT_NAME, DEFAULT_DSH_HOME, bind_agent_to_orchestrator, register_agent  # noqa: E402


def apply_llm_overrides() -> None:
    judge = os.environ.get("TAU2_JUDGE_LLM")
    if not judge:
        return
    import tau2.config as config
    import tau2.evaluator.evaluator_nl_assertions as judge_module

    config.DEFAULT_LLM_NL_ASSERTIONS = judge
    judge_module.DEFAULT_LLM_NL_ASSERTIONS = judge


def load_workspace_api_keys(dsh_home: Path) -> int:
    """Export provider keys from the workspace-local credential store's ``refs:`` block when not already set."""
    path = dsh_home / ".credentials.yaml"
    if not path.is_file():
        return 0
    exported = 0
    in_refs = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("refs:"):
            in_refs = True
            continue
        if not in_refs:
            continue
        if not line.startswith(" "):
            in_refs = False
            continue
        match = re.match(r"\s+([A-Z][A-Z0-9_]+):\s*(\S+)\s*$", line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = match.group(2).strip("'\"")
            exported += 1
    return exported


def main() -> None:
    apply_llm_overrides()
    dsh_home = Path(os.environ.get("TAU2_DSH_HOME") or DEFAULT_DSH_HOME)
    load_workspace_api_keys(dsh_home)
    register_agent(AGENT_NAME)
    bind_agent_to_orchestrator()
    from tau2.cli import main as tau2_main

    tau2_main()


if __name__ == "__main__":
    main()
