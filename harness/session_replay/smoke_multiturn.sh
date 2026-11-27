#!/usr/bin/env bash
# Real-runtime smoke test of the multi-turn SAC fix.
#
# Boots the workspace-local headless profile `sac-default` (its bundle is linked
# to the working-tree plugin) for three turns of one session, each turn with a
# new request, then leaves the session log under work/sac-bug/smoke/ for
# harness/session_replay/replay.mjs and simulate.mjs.
#
# Nothing touches the real DSH home: DSH_HOME is work/dshhome (the same
# workspace-local home the benchmark harness uses) and the agent runs in a
# throw-away workspace. It does call the profile's model (GLM-5.3-Inner via the
# JMS proxy key stored in that home's credential store).
#
#   bash harness/session_replay/smoke_multiturn.sh [turns=3]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DSH="${DSH:-/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh}"
# Always the workspace-local home. Inside a DSH session the environment carries
# the real DSH_HOME (~/.dsh); inheriting it would look the profile up — and run
# the agent — against the user's real profile and session tree.
export DSH_HOME="$ROOT/work/dshhome"
PROFILE="${SAC_SMOKE_PROFILE:-sac-default}"
TURNS="${1:-3}"

OUT="$ROOT/work/sac-bug/smoke"
WS="$ROOT/work/sac-bug/smoke-ws"
rm -rf "$OUT" "$WS"
mkdir -p "$OUT" "$WS/notes"
printf 'project marker: alpha-111\n' > "$WS/notes/a.txt"
printf 'project marker: beta-222\n'  > "$WS/notes/b.txt"
printf 'project marker: gamma-333\n' > "$WS/notes/c.txt"

if [ -z "${JMS_PROXY_API_KEY:-}" ] && [ -f "$DSH_HOME/.credentials.yaml" ]; then
  JMS_PROXY_API_KEY="$(python3 -c "
import re, sys
text = open(sys.argv[1], encoding='utf-8').read()
match = re.search(r'JMS_PROXY_API_KEY:\s*(\S+)', text)
print(match.group(1).strip().strip('\\\"\\'') if match else '')" "$DSH_HOME/.credentials.yaml")"
  export JMS_PROXY_API_KEY
fi
export DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-workspace-write}"

PROMPTS=(
  "依次读取 notes/a.txt、notes/b.txt、notes/c.txt（每个文件单独调用一次 read，分三步进行），最后列出三个文件里的标记。"
  "换个任务，前面的不用管了：在当前目录新建 summary.txt，文件里只写一行 hello-multi-turn，然后读出来确认。"
  "再改一下：把 summary.txt 的内容改成 goodbye-single-anchor（仍然只有一行），读出来确认，并告诉我这一行是什么。"
)

SESSION_ID=""
for index in $(seq 0 $((TURNS - 1))); do
  prompt="${PROMPTS[$index]}"
  args=(--profile "$PROFILE" --json --permission-mode "$DSH_PERMISSION_MODE" --approval-policy never)
  if [ -n "$SESSION_ID" ]; then args+=(--session-id "$SESSION_ID"); fi
  echo "=== turn $((index + 1)): $prompt"
  (cd "$WS" && "$DSH" "${args[@]}" "$prompt" > "$OUT/turn$((index + 1)).jsonl" 2> "$OUT/turn$((index + 1)).stderr.log") || echo "turn $((index + 1)) exited with $?"
  if [ -z "$SESSION_ID" ]; then
    SESSION_ID="$(python3 - "$OUT/turn1.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding='utf-8'):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    for key in ('sessionId', 'session', 'id'):
        value = event.get(key) if isinstance(event, dict) else None
        if isinstance(value, str) and value.startswith('session-'):
            print(value); raise SystemExit
    print('', end='')
PY
)"
  fi
  echo "    session: ${SESSION_ID:-<unknown>}"
  grep -h "dsh-sac-compact" "$OUT/turn$((index + 1)).stderr.log" | tail -4 | sed 's/^/    /' || true
done

echo "session id: $SESSION_ID"
LOG="$(find "$DSH_HOME/sessions" -name 'session.v4.jsonl*' -path "*${SESSION_ID}*" 2>/dev/null | head -1 || true)"
if [ -n "$LOG" ] && [[ "$LOG" == *.zstd ]]; then
  zstd -dc "$LOG" > "$OUT/session.v4.jsonl"
  LOG="$OUT/session.v4.jsonl"
fi
echo "session log: ${LOG:-<not found>}"
echo "next: node harness/session_replay/replay.mjs <log> new   /   node harness/session_replay/simulate.mjs <log> both"
