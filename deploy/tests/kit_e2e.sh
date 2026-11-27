#!/bin/bash
# kit_e2e.sh - end-to-end regression test of the SAC deploy kit in throw-away DSH homes.
#
#   deploy/tests/kit_e2e.sh [path-to-kit-directory]
#
# Default kit: deploy/dist/sac-deploy-kit-v<version of plugins/dsh-sac-compact>.
# Nothing touches the real ~/.dsh: every scenario gets its own DSH root under a temp dir.
# Scenarios that start from the previous v1.1.0 kit need OLD_KIT (the path of an unpacked
# v1.1.0 kit); they are skipped when it is not set or not there.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VERSION="$(python3 -c "import json;print(json.load(open('$REPO/plugins/dsh-sac-compact/package.json'))['version'])")"
KIT="${1:-$REPO/deploy/dist/sac-deploy-kit-v$VERSION}"
OLD_KIT="${OLD_KIT:-}"
T="$(mktemp -d /tmp/sac-kit-e2e.XXXXXX)"
DSH_CLI="/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS + 1)); echo "  ok   $*"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL $*"; }
skip() { SKIP=$((SKIP + 1)); echo "  skip $*"; }
title() { echo; echo "### $*"; }
# expect_exit <code> <label> -- <command...>: run, compare the exit code, keep the output
expect_exit() {
  local want="$1" label="$2"; shift 3
  OUT="$("$@" 2>&1)"; GOT=$?
  if [ "$GOT" = "$want" ]; then ok "$label (exit $GOT)"; else bad "$label: exit $GOT, expected $want"; echo "$OUT" | sed 's/^/       | /' | tail -15; fi
}
has()    { if printf '%s\n' "$OUT" | grep -qF -- "$1"; then ok "output has: $1"; else bad "output lacks: $1"; printf '%s\n' "$OUT" | tail -6 | sed 's/^/       | /'; fi; }
hasnot() { if printf '%s\n' "$OUT" | grep -qF -- "$1"; then bad "output should not have: $1"; else ok "output lacks: $1"; fi; }
upd()   { node "$KIT/update-sac.js" "$@"; }
diag()  { node "$KIT/diagnose-sac.js" "$@"; }

mkprofile() {  # mkprofile <dsh-root> [name]
  local p="$1/profiles/${2:-desktop}"; mkdir -p "$p"
  printf '{\n  "name": "dsh-profile-${2:-desktop}",\n  "private": true,\n  "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"] } },\n  "dependencies": {}\n}\n' > "$p/package.json"
  echo "[]" > "$p/cordis.yml"
  printf "lockfileVersion: '9.0'\n\nsettings:\n  autoInstallPeers: false\n  excludeLinksFromLockfile: false\n\nimporters:\n\n  .:\n    dependencies:\n\npackages:\n\nsnapshots:\n\n" > "$p/pnpm-lock.yaml"
}
ver_of() { grep -h '"version"' "$1" | head -1 | sed 's/[^0-9.]//g'; }

[ -f "$KIT/update-sac.js" ] || { echo "kit not found: $KIT (build it: python3 tools/build_sac_deploy_kit.py)" >&2; exit 2; }
[ -f "$OLD_KIT/install-sac.js" ] || { echo "note: old kit not found at $OLD_KIT; scenarios that upgrade from v1.1.0 are skipped" >&2; }
echo "kit: $KIT"; echo "tmp: $T"

if [ -f "$OLD_KIT/install-sac.js" ]; then
  title "S1 v1.1.0 (preset method) -> check -> update -> idempotent -> diagnose -> rollback -> undo"
  H="$T/s1"; mkprofile "$H"; node "$OLD_KIT/install-sac.js" -dsh-root "$H" -profile desktop > /dev/null 2>&1 || bad "old kit install"
  cp "$H/profiles/desktop/package.json" "$T/s1-package.before.json"; cp "$H/profiles/desktop/pnpm-lock.yaml" "$T/s1-lock.before.yaml"
  [ "$(ver_of "$H/plugins/sac/dsh-sac-compact/package.json")" = "1.1.0" ] && ok "old state: 1.1.0 installed" || bad "old state not 1.1.0"
  expect_exit 10 "check: update needed" -- upd -check -dsh-root "$H" -profile desktop; has "preset-patch=legacy"; has "dsh-sac-compact.version=1.1.0"
  [ -d "$H/sac-backups" ] && bad "-check created a backup" || ok "-check changed nothing (no backup dir)"
  expect_exit 10 "diagnose before: findings" -- diag -dsh-root "$H" -profile desktop; has "this kit installs $VERSION"
  expect_exit 0 "update" -- upd -yes -dsh-root "$H" -profile desktop
  has "all 18 files match the kit"; has "self-test:"; has "update complete: SAC $VERSION, preset method"; has "generated from this client's standard preset"
  [ "$(ver_of "$H/plugins/sac/dsh-sac-compact/package.json")/$VERSION" = "$VERSION/$VERSION" ] && ok "compact now $VERSION" || bad "compact version"
  diff -rq "$KIT/plugins/dsh-sac-compact" "$H/plugins/sac/dsh-sac-compact" > /dev/null && ok "compact files identical to the kit" || bad "compact files differ from the kit"
  grep -qF "link:$(python3 -c "import json;print(json.load(open('$T/s1-package.before.json'))['dependencies']['@local/dsh-sac-preset'][5:])")" "$H/profiles/desktop/package.json" && ok "existing link: path in package.json preserved" || bad "package.json link path changed"
  grep -qF "dsh-sac-preset" "$H/profiles/desktop/pnpm-lock.yaml" && ok "lockfile has the entry" || bad "lockfile lacks the entry"
  B="$(ls "$H/sac-backups" | tail -1)"
  expect_exit 0 "check: up to date" -- upd -check -dsh-root "$H" -profile desktop; has "up-to-date=true"
  expect_exit 0 "update again: nothing to do" -- upd -yes -dsh-root "$H" -profile desktop; has "already installed and up to date"
  [ "$(ls "$H/sac-backups" | wc -l | tr -d ' ')" = "1" ] && ok "no extra backup on a no-op" || bad "a no-op created a backup"
  expect_exit 0 "diagnose after: clean" -- diag -dsh-root "$H" -profile desktop; has "config = the policy this kit ships"; has "VERDICT"; hasnot "[FAIL]"
  expect_exit 0 "rollback" -- upd -rollback -dsh-root "$H" -profile desktop; has "rollback complete"
  diff -rq "$H/sac-backups/$B/plugins/dsh-sac-compact" "$H/plugins/sac/dsh-sac-compact" > /dev/null && diff -rq "$H/sac-backups/$B/plugins/dsh-sac-preset" "$H/plugins/sac/dsh-sac-preset" > /dev/null && ok "plugin files back to the backup, byte for byte" || bad "rollback: files differ from the backup"
  cmp -s "$T/s1-package.before.json" "$H/profiles/desktop/package.json" && ok "package.json back to the original" || bad "rollback: package.json differs"
  cmp -s "$T/s1-lock.before.yaml" "$H/profiles/desktop/pnpm-lock.yaml" && ok "lockfile back to the original" || bad "rollback: lockfile differs"
  expect_exit 10 "check after rollback: update needed" -- upd -check -dsh-root "$H" -profile desktop; has "dsh-sac-compact.version=1.1.0"
  U="$(ls -d "$H"/sac-backups-undo/*/ | tail -1)"
  expect_exit 0 "undo the rollback (-backup <undo dir>)" -- upd -rollback -backup "$U" -dsh-root "$H" -profile desktop
  expect_exit 0 "check after undo: up to date" -- upd -check -dsh-root "$H" -profile desktop

  title "S2 v1.1.0 installed profile-wide (global) keeps the global method"
  H="$T/s2"; mkprofile "$H"; node "$OLD_KIT/install-sac.js" -dsh-root "$H" -profile desktop -global > /dev/null 2>&1 || bad "old kit install (global)"
  expect_exit 10 "check" -- upd -check -dsh-root "$H" -profile desktop; has "mount: global"; has "registered=global"
  expect_exit 0 "update keeps global" -- upd -yes -dsh-root "$H" -profile desktop; has "update complete: SAC $VERSION, global method"; has "no preset is registered"
  python3 - "$H" <<'PY' && ok "profile-wide registration is right" || bad "profile-wide registration"
import json, os, sys
h = sys.argv[1]; d = json.load(open(os.path.join(h, "profiles", "desktop", "package.json")))
b = d["dsh"]["profile"]["bundles"]; deps = d["dependencies"]
assert "@local/dsh-sac-compact" in b and "@local/dsh-sac-preset" not in b, b
assert deps.get("@local/dsh-sac-compact", "").startswith("link:") and "@local/dsh-sac-preset" not in deps, deps
assert os.path.exists(os.path.join(h, "profiles", "desktop", "node_modules", "@local", "dsh-sac-compact", "package.json"))
PY
  cmp -s "$KIT/plugins/dsh-sac-compact/cordis.patch.yml" "$H/plugins/sac/dsh-sac-compact/cordis.patch.yml" && ok "the bundle's own (shipped) config is what runs" || bad "bundle patch differs"
  expect_exit 0 "check" -- upd -check -dsh-root "$H" -profile desktop
  expect_exit 0 "diagnose" -- diag -dsh-root "$H" -profile desktop; has "profile-wide"; hasnot "[FAIL]"

  title "S4 a failing step rolls the update back by itself"
  H="$T/s4"; mkprofile "$H"; node "$OLD_KIT/install-sac.js" -dsh-root "$H" -profile desktop > /dev/null 2>&1 || bad "old kit install"
  cp "$H/profiles/desktop/package.json" "$T/s4-package.before.json"
  chmod 000 "$H/profiles/desktop/pnpm-lock.yaml"   # step 6 cannot read it (restored below); the backup must skip it
  expect_exit 2 "update fails" -- upd -yes -dsh-root "$H" -profile desktop; has "restoring the previous state"; has "update aborted; the previous state is back"
  B="$(ls "$H/sac-backups" | tail -1)"
  diff -rq "$H/sac-backups/$B/plugins/dsh-sac-compact" "$H/plugins/sac/dsh-sac-compact" > /dev/null && diff -rq "$H/sac-backups/$B/plugins/dsh-sac-preset" "$H/plugins/sac/dsh-sac-preset" > /dev/null && ok "old files restored" || bad "auto-rollback: files differ"
  cmp -s "$T/s4-package.before.json" "$H/profiles/desktop/package.json" && ok "package.json restored" || bad "auto-rollback: package.json differs"
  chmod 644 "$H/profiles/desktop/pnpm-lock.yaml"; cp "$T/s1-lock.before.yaml" "$H/profiles/desktop/pnpm-lock.yaml"
  expect_exit 0 "and the update succeeds once the cause is gone" -- upd -yes -dsh-root "$H" -profile desktop
fi

title "S3 fresh install"
H="$T/s3"; mkprofile "$H"
expect_exit 10 "check: not registered" -- upd -check -dsh-root "$H" -profile desktop; has "SAC is not registered"; has "mount=preset"
expect_exit 0 "install" -- upd -yes -dsh-root "$H" -profile desktop; has "install complete: SAC $VERSION, preset method"
[ -L "$H/profiles/desktop/node_modules/@local/dsh-sac-preset" ] && ok "node_modules link created" || bad "node_modules link missing"
grep -qF "\"@local/dsh-sac-preset\": \"link:$H/plugins/sac/dsh-sac-preset\"" "$H/profiles/desktop/package.json" && ok "package.json records the plugins dir as given (no symlink-resolved path)" || bad "package.json link: $(grep -n 'link:' "$H/profiles/desktop/package.json")"
grep -qF "version: link:../../plugins/sac/dsh-sac-preset" "$H/profiles/desktop/pnpm-lock.yaml" && ok "lockfile entry is relative, as pnpm writes it" || bad "lockfile entry: $(grep -A2 dsh-sac-preset "$H/profiles/desktop/pnpm-lock.yaml" | tr '\n' ' ')"
expect_exit 0 "check" -- upd -check -dsh-root "$H" -profile desktop
expect_exit 0 "diagnose" -- diag -dsh-root "$H" -profile desktop; hasnot "[FAIL]"

title "S5 non-interactive without -yes changes nothing"
H="$T/s5"; mkprofile "$H"
expect_exit 2 "refuses" -- upd -dsh-root "$H" -profile desktop < /dev/null; has "stdin is not a terminal: pass -yes"
[ -e "$H/plugins" ] || [ -z "$(ls "$H/plugins" 2>/dev/null)" ] && ok "nothing written" || bad "something was written"

title "S6 uninstall, reinstall, purge"
H="$T/s6"; cp -R "$T/s3" "$H"
expect_exit 0 "uninstall" -- upd -uninstall -yes -dsh-root "$H" -profile desktop; has "uninstall complete"
grep -q "dsh-sac" "$H/profiles/desktop/package.json" && bad "package.json still mentions SAC" || ok "package.json clean"
[ -e "$H/profiles/desktop/node_modules/@local/dsh-sac-preset" ] && bad "link still there" || ok "link removed"
[ -f "$H/plugins/sac/dsh-sac-compact/package.json" ] && ok "plugin files kept (no -purge)" || bad "plugin files were deleted without -purge"
expect_exit 0 "reinstall" -- upd -yes -dsh-root "$H" -profile desktop
expect_exit 0 "uninstall -purge" -- upd -uninstall -purge -yes -dsh-root "$H" -profile desktop
[ -e "$H/plugins/sac/dsh-sac-compact" ] || [ -z "$(ls "$H/plugins/sac/dsh-sac-compact" 2>/dev/null)" ] && ok "plugin files deleted" || bad "plugin files still there"

title "S7-S8, S12 refusals"
H="$T/s7"; mkprofile "$H"
expect_exit 3 "rollback with no backup" -- upd -rollback -yes -dsh-root "$H" -profile desktop; has "no backup found"
expect_exit 1 "unknown profile" -- upd -check -dsh-root "$H" -profile nosuch; has "does not exist"
expect_exit 1 "conflicting modes" -- upd -check -rollback -dsh-root "$H" -profile desktop
mkdir -p "$H/plugins/sac/dsh-sac-compact" && printf '{"name":"someone-elses-package","version":"0.0.1"}' > "$H/plugins/sac/dsh-sac-compact/package.json"
expect_exit 2 "refuses to overwrite a directory that is not the SAC package" -- upd -yes -dsh-root "$H" -profile desktop; has "refusing to touch"
[ -f "$H/plugins/sac/dsh-sac-compact/package.json" ] && grep -q someone-elses "$H/plugins/sac/dsh-sac-compact/package.json" && ok "that directory was left alone" || bad "foreign directory was modified"

title "S9 Windows branches (process.platform reported as win32)"
H="$T/s9"; mkprofile "$H"
printf "Object.defineProperty(process, 'platform', { value: 'win32' });\n" > "$T/fakewin.js"
ASAR="/Applications/DeepSeek Harness.app/Contents/Resources/app.asar"; EXTRA=(); [ -f "$ASAR" ] && EXTRA=(-app-asar "$ASAR")
NODE_OPTIONS="--require $T/fakewin.js" node "$KIT/update-sac.js" -yes -dsh-root "$H" -profile desktop "${EXTRA[@]}" > "$T/s9.out" 2>&1; C=$?
[ "$C" = 0 ] && ok "install with the win32 code paths (exit 0)" || { bad "win32 paths: exit $C"; sed 's/^/       | /' "$T/s9.out" | tail -8; }
grep -q "junction" "$T/s9.out" && ok "win32 wording used" || bad "win32 wording missing"
[ -L "$H/profiles/desktop/node_modules/@local/dsh-sac-preset" ] && ok "link created" || bad "link missing"

title "S10 macOS launcher"
H="$T/s10"; cp -R "$T/s3" "$H"
[ -x "$KIT/update-sac.sh" ] && chmod +x "$KIT/update-sac.sh" 2>/dev/null
expect_exit 0 "./update-sac.sh -check" -- "$KIT/update-sac.sh" -check -dsh-root "$H" -profile desktop; has "Using node"

if [ -n "${DSH_NODE_EXECUTABLE:-}" ] && [ -n "${DSH_INSTALL_DIR:-}" ] && [ -x "$DSH_CLI" ]; then
  title "S11 the real DSH loader composes a profile that uses the kit's files"
  H="$T/s11"; mkprofile "$H" loader; cp -R "$T/s3/plugins" "$H/plugins"
  sed "s#link:$T/s3/plugins/sac/dsh-sac-preset#link:$H/plugins/sac/dsh-sac-preset#" "$T/s3/profiles/desktop/package.json" > "$H/profiles/loader/package.json"
  mkdir -p "$H/profiles/loader/node_modules/@local" && ln -s "$H/plugins/sac/dsh-sac-preset" "$H/profiles/loader/node_modules/@local/dsh-sac-preset"
  ( cd "$T" && DSH_HOME="$H" "$DSH_CLI" --profile loader --dump-config > "$T/s11.yml" 2> "$T/s11.err" ); C=$?
  [ "$C" = 0 ] && ok "dsh --dump-config (exit 0)" || { bad "dsh --dump-config: exit $C"; head -3 "$T/s11.err" | sed 's/^/       | /'; }
  cat > "$T/s11check.cjs" <<'EOF'
const { createRequire } = require('node:module'); const path = require('node:path'); const fs = require('node:fs')
const req = createRequire(path.join(process.env.DSH_INSTALL_DIR, 'package.json')); const YAML = req('yaml')
const flat = []; const walk = (n) => { if (Array.isArray(n)) n.forEach(walk); else if (n && typeof n === 'object') { flat.push(n); if (Array.isArray(n.config)) n.config.forEach(walk) } }
walk(YAML.parse(fs.readFileSync(process.argv[2], 'utf8')))
const sac = flat.find((n) => n.id === 'preset-sac'); if (!sac) { console.log('preset-sac missing'); process.exit(1) }
const entry = sac.config.plugins.find((p) => p.id === 'compaction').config.find((p) => p.id === 'sac-compact')
const shipped = find(fs.readFileSync(process.argv[3], 'utf8'))
function find(text) { const doc = YAML.parse(text)[0].insert; return doc.find((p) => p.id === 'sac-compact').config }
const same = JSON.stringify(entry.config) === JSON.stringify(shipped)
console.log('preset-sac plugins=' + sac.config.plugins.length + ' config-identical=' + same + ' foldTrigger=' + entry.config.foldTrigger)
process.exit(same && entry.name === '@local/dsh-sac-preset/sac' ? 0 : 1)
EOF
  ELECTRON_RUN_AS_NODE=1 "$DSH_NODE_EXECUTABLE" "$T/s11check.cjs" "$T/s11.yml" "$KIT/plugins/dsh-sac-compact/cordis.patch.yml" > "$T/s11check.out" 2>/dev/null; C=$?
  [ "$C" = 0 ] && ok "$(cat "$T/s11check.out")" || { bad "composed tree check (exit $C)"; cat "$T/s11check.out" | sed 's/^/       | /'; }
else
  title "S11 real DSH loader"; skip "DSH runtime not available in this environment"
fi

echo; echo "=== $PASS passed, $FAIL failed, $SKIP skipped ==="
rm -rf "$T"
[ "$FAIL" = 0 ]
