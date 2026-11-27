#!/usr/bin/env bash
# The official `tau2 run` with its built-in `llm_agent` (no DSH, no compaction): the reference for
# "what the official script itself scores on this setup". Its regime is FHA (full history).
#
#   TAG=full4 DOMAINS="airline retail" CONCURRENCY=2 bash harness/tau2/run_official.sh
#   DRY_RUN=1 bash harness/tau2/run_official.sh
#
# The agent model must be reachable through τ²'s own non-streaming litellm call. Defaults: the
# `qwen3.8-27b` of the OpenAI-compatible endpoint configured in benchmarks/tau2-bench/.env for the
# agent, the user simulator and the NL judge (key from the same .env) — the same model as the DSH arms
# when they are generated with `make_profiles.py --provider <provider> --model qwen3.8-27b`.
#
#   AGENT_LLM    litellm model string for the agent (default: openai/qwen3.8-27b)
#   TAG, DOMAINS, TRIALS, CONCURRENCY, NUM_TASKS, USER_LLM, TAU2_JUDGE_LLM, REPORT  as in run_matrix.sh
#   (TAU2_JUDGE_LLM defaults to USER_LLM: retail's NL-assertion judge must be reachable on this endpoint,
#    and τ²'s built-in default gpt-4.1-2025-04-14 is not.)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PY="${PY:-$ROOT/work/tau2/venv/bin/python}"
ARM="official-llm"
DOMAINS="${DOMAINS:-airline retail telecom}"
TAG="${TAG:-fullofficial}"
REPORT="${REPORT:-}"
TRIALS="${TRIALS:-1}"
CONCURRENCY="${CONCURRENCY:-3}"
NUM_TASKS="${NUM_TASKS:-}"
DRY_RUN="${DRY_RUN:-0}"
AGENT_LLM="${AGENT_LLM:-openai/qwen3.8-27b}"
USER_LLM="${USER_LLM:-openai/qwen3.8-27b}"
export TAU2_JUDGE_LLM="${TAU2_JUDGE_LLM:-$USER_LLM}"

# The DSH home is irrelevant to the official agent but run.py reads the judge override and keys from it.
export TAU2_DSH_HOME="${TAU2_DSH_HOME:-$ROOT/work/dshhome}"

for domain in $DOMAINS; do
  split=base
  [ "$domain" = telecom ] && split=small
  limit=()
  [ -n "$NUM_TASKS" ] && limit=(--num-tasks "$NUM_TASKS")
  name="${TAG}_${ARM}_${domain}"
  args=(run --domain "$domain" --task-split-name "$split" "${limit[@]+"${limit[@]}"}"
        --agent llm_agent --agent-llm "$AGENT_LLM"
        --user-llm "$USER_LLM" --num-trials "$TRIALS" --max-concurrency "$CONCURRENCY"
        --seed 300 --save-to "$name" --auto-resume)
  if [ "$DRY_RUN" = "1" ]; then
    echo "$PY -m harness.tau2.run ${args[*]}"
  else
    echo "=== $name  ($(date '+%H:%M:%S'))"
    "$PY" -m harness.tau2.run "${args[@]}"
  fi
done
[ "$DRY_RUN" = "1" ] && exit 0
dirs=()
for domain in $DOMAINS; do dirs+=("benchmarks/tau2-bench/data/simulations/${TAG}_${ARM}_${domain}"); done
report="${REPORT:-work/tau2/reports/${TAG}_${ARM}.md}"
mkdir -p "$(dirname "$report")"
"$PY" -m harness.tau2.analyze "${dirs[@]}" --out "$report"
