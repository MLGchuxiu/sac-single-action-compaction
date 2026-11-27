#!/bin/bash
# diagnose-sac.sh - read-only SAC deployment report (macOS / Linux).
#   ./diagnose-sac.sh [-profile NAME] [-dsh-root PATH] [-app-asar PATH]
# Nothing is modified. Copy the whole output back for analysis.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
JS="$DIR/diagnose-sac.js"
[ -f "$JS" ] || { echo "[SAC] ERROR: diagnose-sac.js was not found next to this script." >&2; exit 1; }

works() { [ -n "$1" ] && [ -x "$1" ] && "$1" -e 'process.exit(0)' >/dev/null 2>&1; }

if works "$(command -v node 2>/dev/null || true)"; then
  exec "$(command -v node)" "$JS" "$@"
fi
for app in "/Applications/DeepSeek Harness.app" "$HOME/Applications/DeepSeek Harness.app"; do
  bundled="$app/Contents/Resources/runtime/primary-runtime/dependencies/node/bin/node"
  if works "$bundled"; then exec "$bundled" "$JS" "$@"; fi
done
for app in "/Applications/DeepSeek Harness.app" "$HOME/Applications/DeepSeek Harness.app"; do
  electron="$app/Contents/MacOS/DeepSeek Harness"
  if [ -x "$electron" ]; then ELECTRON_RUN_AS_NODE=1 exec "$electron" "$JS" "$@"; fi
done
echo "[SAC] ERROR: no node found. Install Node.js 18 or newer, or install DeepSeek Harness (it ships one)." >&2
exit 1
