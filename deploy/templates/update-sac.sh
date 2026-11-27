#!/bin/bash
# update-sac.sh - runs update-sac.js with a usable node (macOS / Linux).
#
#   ./update-sac.sh                install / update the "desktop" profile
#   ./update-sac.sh -check         report only, change nothing
#   ./update-sac.sh -rollback      undo the last update
#   ./update-sac.sh -help          all options
#
# Node is taken from PATH, else from the copy DeepSeek Harness ships, else the
# client's own binary is used as a node runtime. Keep this file next to update-sac.js.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
JS="$DIR/update-sac.js"
[ -f "$JS" ] || { echo "[SAC] ERROR: update-sac.js was not found next to this script." >&2; exit 1; }

works() { [ -n "$1" ] && [ -x "$1" ] && "$1" -e 'process.exit(0)' >/dev/null 2>&1; }

NODE_EXE=""
if works "$(command -v node 2>/dev/null || true)"; then
  NODE_EXE="$(command -v node)"
else
  for app in "/Applications/DeepSeek Harness.app" "$HOME/Applications/DeepSeek Harness.app"; do
    bundled="$app/Contents/Resources/runtime/primary-runtime/dependencies/node/bin/node"
    if works "$bundled"; then NODE_EXE="$bundled"; break; fi
  done
fi

if [ -n "$NODE_EXE" ]; then
  echo "[SAC] Using node: $NODE_EXE ($("$NODE_EXE" --version))"
  exec "$NODE_EXE" "$JS" "$@"
fi

for app in "/Applications/DeepSeek Harness.app" "$HOME/Applications/DeepSeek Harness.app"; do
  electron="$app/Contents/MacOS/DeepSeek Harness"
  if [ -x "$electron" ]; then
    echo "[SAC] Using the DeepSeek Harness runtime as node"
    ELECTRON_RUN_AS_NODE=1 exec "$electron" "$JS" "$@"
  fi
done

echo "[SAC] ERROR: no node found. Install Node.js 18 or newer, or install DeepSeek Harness (it ships one)." >&2
exit 1
