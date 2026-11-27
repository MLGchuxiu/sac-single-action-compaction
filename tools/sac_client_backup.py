#!/usr/bin/env python3
"""Snapshot the SAC plugin as the DeepSeek Harness client loads it right now.

The client does not keep its own copy of SAC: the desktop profile links
``@local/dsh-sac-preset`` to ``plugins/dsh-sac-preset`` in this repository, and
that preset imports ``plugins/dsh-sac-compact`` by a relative path. So "the SAC
plugin inside the client" is whatever those two directories hold at this moment,
plus the profile registration that points at them.

The snapshot is a byte-for-byte copy of both directories and of the profile's
registration files, with a SHA-256 manifest and a restore script. Nothing in
the profile or the repository is modified.

Usage:
    python3 tools/sac_client_backup.py [--out backup/sac-client-<stamp>]
                                       [--profile desktop] [--note "..."]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRESET = "dsh-sac-preset"
COMPACT = "dsh-sac-compact"
PROFILE_FILES = ["package.json", "cordis.yml", "cordis.patch.yml", "pnpm-lock.yaml", "pnpm-workspace.yaml"]
SKIP_DIRS = {"node_modules", "__pycache__", ".DS_Store"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return ""


def copy_tree(src: Path, dst: Path, entries: list[dict]) -> None:
    for item in sorted(src.iterdir()):
        if item.name in SKIP_DIRS:
            continue
        if item.is_dir():
            copy_tree(item, dst / item.name, entries)
            continue
        target = dst / item.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        entries.append({"path": str(target), "size": item.stat().st_size, "sha256": sha256(target),
                        "mtime": dt.datetime.fromtimestamp(item.stat().st_mtime).isoformat(timespec="seconds")})


def read_version(package: Path) -> str | None:
    try:
        return json.loads(package.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_out = REPO / "backup" / ("sac-client-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--out", type=Path, default=default_out)
    parser.add_argument("--profile", default=os.environ.get("DSH_PROFILE", "desktop"))
    parser.add_argument("--dsh-home", type=Path, default=Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")))
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    out: Path = args.out if args.out.is_absolute() else REPO / args.out
    profile_dir = args.dsh_home / "profiles" / args.profile
    if not profile_dir.is_dir():
        print(f"profile not found: {profile_dir}", file=sys.stderr)
        return 2
    if out.exists():
        print(f"refusing to overwrite an existing snapshot: {out}", file=sys.stderr)
        return 2

    # What the profile registers, and where its link really points.
    package = json.loads((profile_dir / "package.json").read_text(encoding="utf-8"))
    bundles = package.get("dsh", {}).get("profile", {}).get("bundles", [])
    deps = package.get("dependencies", {})
    link = profile_dir / "node_modules" / "@local" / PRESET
    link_target = os.readlink(link) if link.is_symlink() else None
    link_real = str(link.resolve()) if link.exists() else None
    # The preset must resolve to this repository for the snapshot to be "the client's".
    preset_src = REPO / "plugins" / PRESET
    compact_src = REPO / "plugins" / COMPACT
    if link_real and Path(link_real).resolve() != preset_src.resolve():
        print(f"the profile's link resolves outside this repository: {link_real}", file=sys.stderr)
        return 3

    files: list[dict] = []
    copy_tree(preset_src, out / "plugins" / PRESET, files)
    copy_tree(compact_src, out / "plugins" / COMPACT, files)
    profile_entries: list[dict] = []
    for name in PROFILE_FILES:
        src = profile_dir / name
        if src.is_file():
            (out / "profile" / args.profile).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out / "profile" / args.profile / name)
            profile_entries.append({"path": f"profile/{args.profile}/{name}", "size": src.stat().st_size,
                                    "sha256": sha256(src)})
    for entry in files:
        entry["path"] = str(Path(entry["path"]).relative_to(out)).replace(os.sep, "/")

    install_dir = os.environ.get("DSH_INSTALL_DIR", "")
    dsh_version = None
    if install_dir:
        probe = ("const fs=require('fs');const p=process.argv[1]+'/package.json';"
                 "try{console.log(JSON.parse(fs.readFileSync(p,'utf8')).version)}catch(e){console.log('')}")
        try:
            dsh_version = subprocess.run(
                [os.environ.get("DSH_NODE_EXECUTABLE", "node"), "-e", probe, install_dir],
                env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"}, capture_output=True, text=True, check=False,
            ).stdout.strip() or None
        except OSError:
            dsh_version = None

    manifest = {
        "title": "SAC plugin as loaded by the DeepSeek Harness client",
        "taken_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": args.note,
        "dsh": {"profile": args.profile, "install_dir": install_dir, "version": dsh_version},
        "client_resolution": {
            "profile_dir": str(profile_dir),
            "bundles": bundles,
            "sac_dependencies": {k: v for k, v in deps.items() if "dsh-sac" in k},
            "node_modules_link": str(link),
            "link_target": link_target,
            "link_resolves_to": link_real,
            "imports_compact_from": str(compact_src),
        },
        "versions": {PRESET: read_version(preset_src / "package.json"), COMPACT: read_version(compact_src / "package.json")},
        "repository": {"path": str(REPO), "head": git("rev-parse", "HEAD"),
                       "status_of_plugins": git("status", "--short", "--", "plugins")},
        "counts": {"plugin_files": len(files), "profile_files": len(profile_entries)},
        "files": files + profile_entries,
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "SHA256SUMS").write_text("".join(f"{e['sha256']}  {e['path']}\n" for e in files + profile_entries), encoding="utf-8")

    print(f"snapshot: {out}")
    print(f"preset {manifest['versions'][PRESET]} + compact {manifest['versions'][COMPACT]}, "
          f"{len(files)} plugin files, {len(profile_entries)} profile files")
    print(f"client link: {link} -> {link_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
