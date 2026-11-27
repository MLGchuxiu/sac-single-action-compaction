#!/usr/bin/env bash
# Run the τ²-bench multi-turn matrix (arms × domains) through the official runner with the DSH agent.
#
#   bash harness/tau2/run_matrix.sh                 # full 184-task golden set, SAC + FHA arms
#   TAG=pilot NUM_TASKS=5 bash harness/tau2/run_matrix.sh
#   ARMS=sac DOMAINS=airline TRIALS=3 bash harness/tau2/run_matrix.sh
#   ARMS="sac fha sac-old eocc" bash harness/tau2/run_matrix.sh   # the four-arm comparison
#   DRY_RUN=1 bash harness/tau2/run_matrix.sh       # print the commands, run nothing
#
# Every run is the official `tau2 run` (tasks, user simulator, environment, evaluation) with
# `--agent dsh_agent`; see docs/TEST-PLAN-TAU2-MULTITURN.md.
#
# Environment (all optional unless marked):
#   PY            python with tau2 installed        (default: work/tau2/venv/bin/python)
#   ARMS          space-separated subset of: sac (SAC 1.2.0) fha (full history) sac-old (SAC 1.1.0)
#                 eocc (the paper's threshold compaction, 12K/8K)   (default: "sac fha")
#   DOMAINS       space-separated subset of: airline retail telecom (default: all three)
#   TAG           prefix of the --save-to name      (default: "full"; results land in
#                 benchmarks/tau2-bench/data/simulations/<TAG>_<arm>_<domain>/)
#   REPORT        analysis output file              (default: work/tau2/reports/<TAG>.md; give each
#                 parallel job its own when arms run as separate processes)
#   NUM_TASKS     limit per domain                  (default: all tasks of the split)
#   TRIALS        --num-trials                      (default: 1)
#   CONCURRENCY   --max-concurrency                 (default: 3, the official default)
#   AGENT_MODEL   label recorded in the run metadata (default: the profile's model)
#   USER_LLM      litellm model string for the user simulator  (REQUIRED unless DRY_RUN)
#   TAU2_JUDGE_LLM  litellm model string for the NL-assertion judge (retail needs one;
#                 default is tau2's gpt-4.1-2025-04-14)
#   TAU2_DSH_HOME, TAU2_DSH_RUNS_DIR  see harness/tau2/dsh_agent.py
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PY="${PY:-$ROOT/work/tau2/venv/bin/python}"
ARMS="${ARMS:-sac fha}"
DOMAINS="${DOMAINS:-airline retail telecom}"
TAG="${TAG:-full}"
REPORT="${REPORT:-}"
TRIALS="${TRIALS:-1}"
CONCURRENCY="${CONCURRENCY:-3}"
DRY_RUN="${DRY_RUN:-0}"

# The DSH home is always the workspace-local one: inside a DSH session the
# environment carries the real DSH_HOME (~/.dsh), which benchmark runs must not touch.
export TAU2_DSH_HOME="${TAU2_DSH_HOME:-$ROOT/work/dshhome}"

if [ "$DRY_RUN" != "1" ]; then
  [ -n "${USER_LLM:-}" ] || { echo "USER_LLM is required (litellm model string for the user simulator), e.g. USER_LLM=openai/gpt-4.1" >&2; exit 2; }
  [ -x "$PY" ] || { echo "python not found: $PY" >&2; exit 2; }
  [ -f "$TAU2_DSH_HOME/profiles/tau2-sac/cordis.patch.yml" ] || { echo "profiles missing; run: $PY -m harness.tau2.make_profiles" >&2; exit 2; }
fi

for arm in $ARMS; do
  profile="tau2-$arm"
  for domain in $DOMAINS; do
    split=base
    [ "$domain" = telecom ] && split=small
    limit=()
    [ -n "${NUM_TASKS:-}" ] && limit=(--num-tasks "$NUM_TASKS")
    name="${TAG}_${arm}_${domain}"
    args=(run --domain "$domain" --task-split-name "$split" "${limit[@]+"${limit[@]}"}"
          --agent dsh_agent --agent-llm "${AGENT_MODEL:-dsh/$profile}"
          --agent-llm-args "{\"profile\": \"$profile\"}"
          --user-llm "${USER_LLM:-<USER_LLM>}" --num-trials "$TRIALS" --max-concurrency "$CONCURRENCY"
          --seed 300 --save-to "$name" --auto-resume)
    if [ "$DRY_RUN" = "1" ]; then
      echo "$PY -m harness.tau2.${args[*]}"
    else
      echo "=== $name  ($(date '+%H:%M:%S'))"
      "$PY" -m harness.tau2.run "${args[@]}"
    fi
  done
done

if [ "$DRY_RUN" = "1" ]; then
  echo "$PY -m harness.tau2.analyze <results dirs> --out ${REPORT:-work/tau2/reports/${TAG}.md}"
  exit 0
fi
dirs=()
for arm in $ARMS; do for domain in $DOMAINS; do dirs+=("benchmarks/tau2-bench/data/simulations/${TAG}_${arm}_${domain}"); done; done
report="${REPORT:-work/tau2/reports/${TAG}.md}"
mkdir -p "$(dirname "$report")"
"$PY" -m harness.tau2.analyze "${dirs[@]}" --out "$report"
