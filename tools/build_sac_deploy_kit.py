#!/usr/bin/env python3
"""Build the offline SAC deploy kit from the plugins in this repository.

    python3 tools/build_sac_deploy_kit.py            # build deploy/dist/sac-deploy-kit-v<version>[.zip]
    python3 tools/build_sac_deploy_kit.py --no-zip

The two plugin directories under plugins/ are the only source. The script:
  1. runs their test suites (a failing test stops the build);
  2. reads both cordis.patch.yml files with a real YAML parser and refuses to
     build when the preset's sac-compact config differs from the bundle's
     (the drift that left the GUI preset on the 1.2.0 behaviour);
  3. embeds the plugin files as base64 in update-sac.js, and the parsed config
     as the kit metadata, so the generated preset cannot drift from the files;
  4. writes the kit, a SHA-256 manifest, and a zip.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PLUGINS = REPO / "plugins"
TEMPLATES = REPO / "deploy" / "templates"
DIST = REPO / "deploy" / "dist"
PACKAGE_DIRS = {"dsh-sac-compact", "dsh-sac-preset"}
SKIP = {"node_modules", "__pycache__", ".DS_Store", ".git"}
TOKEN = re.compile(r"__([A-Z_]+)__")


class Loader(yaml.SafeLoader):
    """YAML with the loader's `!!js` tag read as the plain scalar it wraps."""


Loader.add_constructor("tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plugin_files(name: str) -> list[Path]:
    root = PLUGINS / name
    found = sorted(p for p in root.rglob("*") if p.is_file() and not any(part in SKIP for part in p.relative_to(root).parts))
    if not found:
        raise SystemExit(f"no files under {root}")
    return found


def run_tests(name: str) -> tuple[int, int]:
    files = [str(p) for p in plugin_files(name) if p.name.endswith(".test.js")]
    if not files:
        return 0, 0
    run = subprocess.run(["node", "--test", "--test-reporter=tap", *files], cwd=PLUGINS / name,
                         capture_output=True, text=True)
    out = run.stdout + run.stderr
    count = lambda key: int((re.search(rf"^# {key} (\d+)", out, re.M) or [0, 0])[1])
    return count("pass"), count("fail") if run.returncode == 0 else max(count("fail"), 1)


def read_version(name: str) -> str:
    return json.loads((PLUGINS / name / "package.json").read_text(encoding="utf-8"))["version"]


def find_id(node: object, wanted: str) -> dict | None:
    """The first dict with `id == wanted` and a `config`, at any depth (the preset
    keeps sac-compact inside its compaction group, the bundle at the top level)."""
    if isinstance(node, dict):
        if node.get("id") == wanted and "config" in node:
            return node
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = find_id(child, wanted)
        if found:
            return found
    return None


def sac_config(doc: object, label: str) -> dict:
    entry = find_id(doc, "sac-compact")
    if entry is None:
        raise SystemExit(f"{label}: no `sac-compact` entry with a config")
    return entry["config"]


def render(template: str, shared: str, kit: dict, files: dict, version: str, extra: dict | None = None) -> str:
    text = TEMPLATES.joinpath(template).read_text(encoding="utf-8")
    text = text.replace("__SHARED__", shared).replace("__KIT__", json.dumps(kit, ensure_ascii=False, indent=2))
    text = text.replace("__FILES__", json.dumps(files, ensure_ascii=False, indent=0)).replace("__VERSION__", version)
    for key, value in (extra or {}).items():
        text = text.replace(f"__{key}__", str(value))
    left = sorted(set(TOKEN.findall(text)))
    if left:
        raise SystemExit(f"{template}: unreplaced tokens {left}")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--skip-tests", action="store_true", help="do not run the plugin test suites first")
    args = parser.parse_args()

    versions = {name: read_version(name) for name in PACKAGE_DIRS}
    if len(set(versions.values())) != 1:
        raise SystemExit(f"the preset and the plugin must carry the same version: {versions}")
    version = versions["dsh-sac-compact"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"not a plain x.y.z version: {version}")

    passed = failed = 0
    if not args.skip_tests:
        for name in sorted(PACKAGE_DIRS):
            p, f = run_tests(name)
            passed, failed = passed + p, failed + f
            print(f"tests  {name}: {p} passed, {f} failed")
            if f:
                raise SystemExit("a test failed: fix it before packaging")
    print(f"tests  total: {passed} passed")

    bundle = yaml.load((PLUGINS / "dsh-sac-compact" / "cordis.patch.yml").read_text(encoding="utf-8"), Loader=Loader)
    preset = yaml.load((PLUGINS / "dsh-sac-preset" / "cordis.patch.yml").read_text(encoding="utf-8"), Loader=Loader)
    bundle_config, preset_config = sac_config(bundle, "bundle"), sac_config(preset, "preset")
    if bundle_config != preset_config:
        diff = [k for k in set(bundle_config) | set(preset_config) if bundle_config.get(k) != preset_config.get(k)]
        raise SystemExit(f"the preset config differs from the bundle config: {sorted(diff)}")
    if list(bundle_config) != list(preset_config):
        raise SystemExit("the preset config and the bundle config list their keys in a different order")
    if bundle_config.get("foldTrigger") != "size":
        raise SystemExit("the shipped config is not the cost-aware policy (foldTrigger != size)")
    declaration = find_id(preset, "preset-sac")
    if declaration is None:
        raise SystemExit("preset: no preset-sac declaration")
    meta = declaration["config"]
    kit_meta = {"patchId": "preset-sac", "id": meta["id"], "name": meta["name"], "description": meta["description"], "order": meta["order"]}

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    built = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    kit = {"version": version, "builtAt": built, "compactVersion": versions["dsh-sac-compact"],
           "presetVersion": versions["dsh-sac-preset"], "preset": kit_meta, "sacConfig": preset_config}

    files: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for name in sorted(PACKAGE_DIRS):
        for path in plugin_files(name):
            data = path.read_bytes()
            rel = path.relative_to(PLUGINS).as_posix()
            files[rel] = base64.b64encode(data).decode("ascii")
            sizes[rel] = len(data)

    shared = TEMPLATES.joinpath("shared.js").read_text(encoding="utf-8")
    rendered = {
        "update-sac.js": render("update-sac.js.tmpl", shared, kit, files, version),
        "diagnose-sac.js": render("diagnose-sac.js.tmpl", shared, kit, files, version),
        "README.md": render("README.md", shared, kit, files, version, {"TESTS": passed}),
        "AGENT.md": render("AGENT.md", shared, kit, files, version, {"TESTS": passed}),
    }
    copied = ["update-sac.cmd", "diagnose-sac.cmd", "update-sac.sh", "diagnose-sac.sh"]
    for name in copied:
        text = TEMPLATES.joinpath(name).read_text(encoding="utf-8")
        if name.endswith(".cmd"):
            if not text.isascii():
                raise SystemExit(f"{name}: must stay ASCII (a Windows console would garble it)")
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        rendered[name] = text

    out = DIST / f"sac-deploy-kit-v{version}"
    if out.exists():
        # Only ever remove the kit directory this script writes.
        if out.parent.resolve() != DIST.resolve() or not out.name.startswith("sac-deploy-kit-v"):
            raise SystemExit(f"refusing to remove {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name, text in rendered.items():
        target = out / name
        target.write_bytes(text.encode("utf-8"))
        if name.endswith(".sh"):
            target.chmod(0o755)
    for name in sorted(PACKAGE_DIRS):
        shutil.copytree(PLUGINS / name, out / "plugins" / name, ignore=shutil.ignore_patterns(*SKIP))

    for name in ("update-sac.js", "diagnose-sac.js"):
        check = subprocess.run(["node", "--check", str(out / name)], capture_output=True, text=True)
        if check.returncode:
            shutil.rmtree(out)
            raise SystemExit(f"{name} does not parse:\n{check.stderr}")
    for name in ("update-sac.js", "diagnose-sac.js"):
        check = subprocess.run(["node", str(out / name), "-help"], capture_output=True, text=True)
        if check.returncode:
            shutil.rmtree(out)
            raise SystemExit(f"{name} -help failed ({check.returncode}):\n{check.stdout}{check.stderr}")

    manifest_files = [{"path": p.relative_to(out).as_posix(), "bytes": p.stat().st_size, "sha256": sha256(p.read_bytes())}
                      for p in sorted(out.rglob("*")) if p.is_file()]
    manifest = {"kit": "sac-deploy-kit", "version": version, "builtAt": built,
                "plugins": {"@local/dsh-sac-compact": versions["dsh-sac-compact"], "@local/dsh-sac-preset": versions["dsh-sac-preset"]},
                "source": {"directory": str(REPO), "gitHead": head,
                           "pluginsUncommitted": bool(subprocess.run(["git", "status", "--porcelain", "--", "plugins"],
                                                                    cwd=REPO, capture_output=True, text=True).stdout.strip())},
                "tests": {"passed": passed, "failed": failed},
                "payloadFiles": len(files), "files": manifest_files}
    (out / "kit-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"kit    {out}")
    print(f"       {len(manifest_files) + 1} files, payload {len(files)} files ({sum(sizes.values()):,} bytes), "
          f"update-sac.js {sizes and (out / 'update-sac.js').stat().st_size:,} bytes")
    if not args.no_zip:
        archive = DIST / f"{out.name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    info = zipfile.ZipInfo(f"{out.name}/{p.relative_to(out).as_posix()}", date_time=dt.datetime.now().timetuple()[:6])
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (p.stat().st_mode & 0xFFFF) << 16
                    zf.writestr(info, p.read_bytes())
        print(f"zip    {archive} ({archive.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
