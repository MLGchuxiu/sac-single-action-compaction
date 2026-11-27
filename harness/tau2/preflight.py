"""Pre-flight for the τ²-bench multi-turn test: everything is checked *without calling any model*.

    work/tau2/venv/bin/python -m harness.tau2.preflight [--sac] [--dsh-home …] [--quick]

Checks
------
1. τ²-bench: the venv imports ``tau2`` 1.0.1, the data directory resolves, the 184-task golden set loads
   (airline base 50, retail base 114, telecom small 20), the user-simulator guideline files exist.
2. DSH: the CLI runs; the four arm profiles exist and compose; each SAC arm has the SAC bundle
   enabled with the config that defines the arm (see ``EXPECT``) and the bridge plugin; the baseline
   arm has no SAC.
3. The SAC plugin under test is byte-identical to the backup snapshot (``multiturn-1.2.0-pin4``);
   the old arm's plugin is the 1.1.0 extracted from commit e01ac12.
4. Unit tests: SAC plugin (node --test) and the bridge plugin.
5. Credentials — names only, values are never printed: the agent provider key for each profile, and a
   key for the user simulator / NL-assertion judge (litellm: ``OPENAI_API_KEY`` by default).

Exit code 0 means every check passed; 1 means something must be fixed before the pilot run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="ERROR")

from harness.tau2.dsh_agent import DEFAULT_DSH_BIN, DEFAULT_DSH_HOME  # noqa: E402
from harness.tau2.make_profiles import (  # noqa: E402
    BRIDGE,
    DERIVED,
    EOCC_PAPER_WORDING,
    EOCC_THRESHOLD_TOKENS,
    HERMETIC_ROWS,
    OLD_SAC,
    SAC,
    eocc_overrides,
)

SNAPSHOT = ROOT / "work" / "bench" / "plugin_versions" / "multiturn-1.2.0-pin4" / "dsh-sac-compact"
TAU2 = ROOT / "benchmarks" / "tau2-bench"
GOLDEN = {"airline": ("base", 50), "retail": ("base", 114), "telecom": ("small", 20)}
PROFILES = ("tau2-sac", "tau2-fha", "tau2-sac-old", "tau2-eocc", "tau2-eocc-anchored")
# What defines each arm, as literal lines of the composed `sac-compact` config row
# (None = the line must be absent). FHA is "no SAC bundle".
_EOCC = {
    "pinInstructions: false": True,
    f"contextBudgetTokens: {EOCC_THRESHOLD_TOKENS}": True,
    "keepLastObservation: false": True,
    "keepTrailingReply: false": True,
    "statePrompt: paper": True,
}
EXPECT = {
    "tau2-sac": {"maxPinnedInstructions: 4": True, "pinInstructions: true": True, "statePrompt: paper": True},
    "tau2-fha": None,
    "tau2-sac-old": {
        "actionsPerCompaction: 2": True,
        "keepLastObservation: true": True,
        "maxPinnedInstructions": False,
        "pinInstructions": False,
    },
    "tau2-eocc": dict(_EOCC),
    "tau2-eocc-anchored": dict(_EOCC),
}


class Report:
    def __init__(self) -> None:
        self.blockers: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def check(self, ok: bool, what: str, detail: str = "", warn_only: bool = False) -> bool:
        line = f"{'ok  ' if ok else ('WARN' if warn_only else 'FAIL')}  {what}" + (f" — {detail}" if detail else "")
        print(line)
        if not ok:
            (self.warnings if warn_only else self.blockers).append(f"{what}{' — ' + detail if detail else ''}")
        return ok

    def info(self, what: str) -> None:
        print(f"info  {what}")
        self.infos.append(what)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def sh(command: list[str], env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout)


def check_tau2(report: Report) -> None:
    print("\n[1] τ²-bench")
    try:
        import tau2
        from tau2.utils import DATA_DIR

        report.check(tau2.__file__ is not None and str(Path(tau2.__file__).resolve()).startswith(str(TAU2.resolve())), "tau2 imports from the local copy", str(Path(tau2.__file__).parent))
        report.check(Path(DATA_DIR).resolve() == (TAU2 / "data").resolve(), "data directory", str(DATA_DIR))
        from tau2.runner import get_tasks

        total = 0
        for domain, (split, expected) in GOLDEN.items():
            count = len(get_tasks(domain, task_split_name=split))
            total += count
            report.check(count == expected, f"golden set {domain}/{split}", f"{count} tasks (expected {expected})")
        report.check(total == 184, "golden set total", f"{total} (expected 184)")
        for name in ("simulation_guidelines.md", "simulation_guidelines_tools.md"):
            report.check((TAU2 / "data" / "tau2" / "user_simulator" / name).is_file(), f"user simulator guideline {name}")
        version = sh([sys.executable, "-m", "pip", "show", "tau2"]).stdout
        m = re.search(r"^Version: (\S+)", version, re.M)
        report.check(m is not None and m.group(1) == "1.0.1", "tau2 package version", m.group(1) if m else "unknown")
    except Exception as error:  # noqa: BLE001
        report.check(False, "tau2 import / task loading", str(error))


def check_dsh(report: Report, dsh_home: Path, dsh_bin: str, quick: bool) -> None:
    print("\n[2] DSH runtime and profiles")
    report.check(Path(dsh_bin).is_file() and os.access(dsh_bin, os.X_OK), "dsh CLI", dsh_bin)
    env = {k: v for k, v in os.environ.items() if not k.startswith("DSH_")}
    env["DSH_HOME"] = str(dsh_home)
    version = sh([dsh_bin, "--version"], env=env)
    report.check(version.returncode == 0, "dsh --version", version.stdout.strip() or version.stderr.strip()[:120])
    if quick:
        report.info("--quick: skipping profile composition (run without --quick once before the pilot)")
        return
    for name in PROFILES:
        profile = dsh_home / "profiles" / name
        if not report.check(profile.is_dir(), f"profile {name} exists", str(profile)):
            continue
        dump = sh([dsh_bin, "--profile", name, "--dump-config"], env=env, timeout=180)
        if not report.check(dump.returncode == 0, f"profile {name} composes", dump.stderr.strip()[-200:]):
            continue
        text = dump.stdout
        rows = dict(re.findall(r"^- id: ([\w\-]+)\n(?:  name: .*\n)?((?:  .*\n)*)", text, re.M))
        sac = "sac-compact" in rows and "disabled: true" not in rows["sac-compact"]
        expected = EXPECT.get(name)
        if expected is None:
            report.check(not sac, f"{name}: no SAC bundle (full-history baseline)")
        else:
            report.check(sac, f"{name}: SAC bundle enabled")
            if sac:
                for line, required in expected.items():
                    there = line in rows["sac-compact"]
                    report.check(there == required, f"{name}: {'has' if required else 'has no'} `{line}`")
        report.check("tau2-bridge" in rows and "disabled: true" not in rows["tau2-bridge"], f"{name}: tau2 bridge plugin enabled")
        report.check("maxParallelToolCalls: 1" in rows.get("agent-loop", ""), f"{name}: tool calls run one at a time")
        missing = [row for row in HERMETIC_ROWS if row not in rows or "disabled: true" not in rows[row]]
        report.check(not missing, f"{name}: hermetic tool set", f"not disabled: {missing}" if missing else f"{len(HERMETIC_ROWS)} rows disabled")
        info_file = profile / "PROFILE_INFO.json"
        if info_file.is_file():
            info = json.loads(info_file.read_text(encoding="utf-8"))
            report.info(f"{name}: provider={info['provider']} model={info['model']} sac={info.get('sac_path')} version={info.get('sac_version')}")
            key_env = re.search(r"apiKeyEnv: (\S+)", text)
            check_key(report, f"{name}: provider key variable", key_env.group(1).strip("'\"") if key_env else None, dsh_home)


def check_key(report: Report, what: str, name: str | None, dsh_home: Path) -> None:
    if not name:
        report.check(False, what, "profile declares no apiKeyEnv")
        return
    present = bool(os.environ.get(name))
    if not present:
        store = dsh_home / ".credentials.yaml"
        if store.is_file():
            present = re.search(rf"^\s+{re.escape(name)}:\s*\S+", store.read_text(encoding="utf-8"), re.M) is not None
    report.check(present, what, f"{name} is set" if present else f"{name} is neither in the environment nor in {dsh_home / '.credentials.yaml'}")


def check_sac_source(report: Report) -> None:
    print("\n[3] SAC plugin under test vs backup snapshot")
    if not SNAPSHOT.is_dir():
        report.check(False, "backup snapshot", str(SNAPSHOT))
        return
    report.info(f"snapshot: {SNAPSHOT} (commit dfa3b6a)")
    for rel in ("index.js", "lib/sac-policy.js", "cordis.patch.yml", "package.json"):
        report.check(digest(SAC / rel) == digest(SNAPSHOT / rel), f"plugins/dsh-sac-compact/{rel} identical to the snapshot")
    print("\n[3b] old SAC plugin (the pre-fix arm) vs git commit e01ac12")
    if not report.check(OLD_SAC.is_dir(), "old plugin directory", str(OLD_SAC)):
        return
    for rel in ("index.js", "lib/sac-policy.js", "cordis.patch.yml", "package.json"):
        blob = subprocess.run(["git", "show", f"e01ac12:plugins/dsh-sac-compact/{rel}"], cwd=ROOT, capture_output=True)
        report.check(blob.returncode == 0 and blob.stdout == (OLD_SAC / rel).read_bytes(), f"old plugin {rel} identical to e01ac12")
    old_version = json.loads((OLD_SAC / "package.json").read_text(encoding="utf-8")).get("version")
    new_version = json.loads((SAC / "package.json").read_text(encoding="utf-8")).get("version")
    report.check(old_version == "1.1.0" and new_version == "1.2.0", "plugin versions", f"old={old_version} new={new_version}")
    linked = (Path(os.environ.get("TAU2_DSH_HOME") or DEFAULT_DSH_HOME) / "profiles" / "tau2-sac-old" / "node_modules" / "@local" / "dsh-sac-compact").resolve()
    report.check(linked == OLD_SAC.resolve(), "tau2-sac-old links the old plugin", str(linked))

    print("\n[3c] EoCC plugins (derived copies of 1.2.0) vs the snapshot")
    eocc_variants = {"tau2-eocc": f"eocc-paper-1.2.0-thr{EOCC_THRESHOLD_TOKENS}", "tau2-eocc-anchored": f"eocc-1.2.0-thr{EOCC_THRESHOLD_TOKENS}"}
    for profile, folder in eocc_variants.items():
        eocc = DERIVED / folder / "dsh-sac-compact"
        if not report.check(eocc.is_dir(), f"{profile}: derived plugin directory", str(eocc)):
            continue
        paper = profile == "tau2-eocc"
        for rel in ("lib/sac-policy.js", "package.json"):
            report.check(digest(eocc / rel) == digest(SNAPSHOT / rel), f"{profile}: plugin {rel} identical to the snapshot")
        if paper:
            expected = (SNAPSHOT / "index.js").read_text(encoding="utf-8")
            for old, new in EOCC_PAPER_WORDING.items():
                expected = expected.replace(old, new)
            report.check((eocc / "index.js").read_text(encoding="utf-8") == expected,
                         f"{profile}: index.js = snapshot + the paper's Appendix G.1 compaction wording ({len(EOCC_PAPER_WORDING)} replacements)")
        else:
            report.check(digest(eocc / "index.js") == digest(SNAPSHOT / "index.js"), f"{profile}: plugin index.js identical to the snapshot")
        overrides = eocc_overrides()

        def scalars(path: Path) -> dict:
            return dict(re.findall(r"^[ \t]+([A-Za-z][A-Za-z0-9]*): ([^\n#]+?)\s*$", path.read_text(encoding="utf-8"), re.M))

        base, derived = scalars(SNAPSHOT / "cordis.patch.yml"), scalars(eocc / "cordis.patch.yml")
        diff = sorted(key for key in set(base) | set(derived) if base.get(key) != derived.get(key))
        report.check(diff == sorted(overrides), f"{profile}: config differs from the bundle defaults in exactly the EoCC keys", f"differing keys: {diff}")
        report.check(all(derived.get(key) == value for key, value in overrides.items()), f"{profile}: every EoCC key has its intended value")
        linked = (Path(os.environ.get("TAU2_DSH_HOME") or DEFAULT_DSH_HOME) / "profiles" / profile / "node_modules" / "@local" / "dsh-sac-compact").resolve()
        report.check(linked == eocc.resolve(), f"{profile}: links its derived EoCC plugin", str(linked))


def check_tests(report: Report) -> None:
    print("\n[4] unit tests (no model)")
    sac = subprocess.run(["node", "--test", "test/sac-policy.test.js", "test/integration.test.js", "test/multiturn-policy.test.js", "test/multiturn-integration.test.js"], cwd=SAC, capture_output=True, text=True, timeout=300)
    passed = re.search(r"ℹ pass (\d+)", sac.stdout)
    failed = re.search(r"ℹ fail (\d+)", sac.stdout)
    report.check(sac.returncode == 0 and failed is not None and failed.group(1) == "0", "SAC plugin tests", f"pass {passed.group(1) if passed else '?'} / fail {failed.group(1) if failed else '?'}")
    bridge = subprocess.run(["node", "--test", "test/bridge.test.js"], cwd=BRIDGE, capture_output=True, text=True, timeout=120)
    passed = re.search(r"ℹ pass (\d+)", bridge.stdout)
    report.check(bridge.returncode == 0, "bridge plugin tests", f"pass {passed.group(1) if passed else '?'}")


def check_llm_keys(report: Report) -> None:
    print("\n[5] user simulator / NL-assertion judge credentials (names only)")
    keys: dict[str, str] = {}
    for candidate in (ROOT / ".env", TAU2 / ".env"):
        if candidate.is_file():
            report.info(f".env found: {candidate} (tau2 loads it through python-dotenv)")
            for line in candidate.read_text(encoding="utf-8").splitlines():
                match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]+)\s*=\s*(\S+)", line)
                if match:
                    keys[match.group(1)] = match.group(2)
    if not keys:
        report.info("no .env at the repository root or benchmarks/tau2-bench/.env")
    judge = os.environ.get("TAU2_JUDGE_LLM")
    provider = (judge or "gpt-4.1-2025-04-14").split("/")[0] if "/" in (judge or "gpt-4.1-2025-04-14") else "openai"
    env_name = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider, provider.upper() + "_API_KEY")
    present = bool(os.environ.get(env_name)) or bool(keys.get(env_name))
    report.check(present, f"key for the judge / default simulator provider ({provider})", f"{env_name} present" if present else f"{env_name} missing — the pilot cannot run until it is provided", warn_only=True)
    base = os.environ.get("OPENAI_API_BASE") or os.environ.get("OPENAI_BASE_URL") or keys.get("OPENAI_API_BASE") or keys.get("OPENAI_BASE_URL")
    report.info(f"TAU2_JUDGE_LLM={judge or '(unset: gpt-4.1-2025-04-14)'}; OPENAI base URL {'set' if base else 'not set (api.openai.com)'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dsh-home", default=os.environ.get("TAU2_DSH_HOME") or str(DEFAULT_DSH_HOME))
    parser.add_argument("--dsh-bin", default=os.environ.get("TAU2_DSH_BIN") or DEFAULT_DSH_BIN)
    parser.add_argument("--quick", action="store_true", help="skip profile composition")
    args = parser.parse_args()
    dsh_home = Path(args.dsh_home).resolve()
    report = Report()
    print(f"repository: {ROOT}\nDSH home:  {dsh_home}")
    check_tau2(report)
    check_dsh(report, dsh_home, args.dsh_bin, args.quick)
    check_sac_source(report)
    check_tests(report)
    check_llm_keys(report)
    print()
    if report.blockers:
        print(f"NO-GO — {len(report.blockers)} blocker(s):")
        for item in report.blockers:
            print(f"  - {item}")
    else:
        print("GO — every blocking check passed; no model was called.")
    if report.warnings:
        print(f"{len(report.warnings)} warning(s):")
        for item in report.warnings:
            print(f"  - {item}")
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
