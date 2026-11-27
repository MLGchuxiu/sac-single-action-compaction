"""Write the DSH headless profiles used by the τ²-bench multi-turn test into the workspace-local DSH home.

    work/tau2/venv/bin/python -m harness.tau2.make_profiles            # all four arms, GLM-5.3-Inner via JMS proxy
    work/tau2/venv/bin/python -m harness.tau2.make_profiles --only tau2-sac tau2-eocc
    work/tau2/venv/bin/python -m harness.tau2.make_profiles --sac-path work/bench/plugin_versions/multiturn-1.2.0-pin4/dsh-sac-compact
    work/tau2/venv/bin/python -m harness.tau2.make_profiles --provider my-endpoint --model my-model \
        --base-url https://host/v1 --api-key-env MY_KEY --max-tokens 8192 --only tau2-sac

Profiles (the four arms of the comparison; see ``ARMS``)
------------------------------------------------------
tau2-sac      new SAC, plugin 1.2.0 from the workspace (the plugin's own config: paper template,
              maxPinnedInstructions 4) + the τ² bridge plugin.
tau2-fha      FHA, the paper's full-history baseline: the same without the SAC bundle.
tau2-sac-old  old SAC, plugin 1.1.0 exactly as it was before the multi-turn fix (git commit e01ac12,
              extracted to work/bench/plugin_versions/old-1.1.0-e01ac12/), bundle defaults.
tau2-eocc     EoCC, the paper's end-of-conversation compaction (§2.4.1), written with the paper's own
              compaction wording (Appendix G.1): the 1.2.0 plugin with ``pinInstructions: false`` (one
              anchor: the first user message = the original task) and no fold by action count; once the
              surface reaches ``contextBudgetTokens`` everything after the first user message is folded
              into one bounded recap, so the next request is system + original task + compacted state.
              Default threshold 8000 tokens = the paper's 12K/8K setting for τ²-bench (Table 8).
tau2-eocc-anchored  the same regime, but the recap is requested with plugin 1.2.0's wording, which
              anchors the Objective on the first user message (reference arm; not the paper's template).

All are *hermetic*: everything a customer-service agent does not need is disabled (shell, files, web,
subagents, todo, plan mode, goals, skills, workspace instructions, the ask-user tool, the LLM session
title), so the model sees the τ² policy and exactly the τ² domain tools — the same information the
official ``llm_agent`` gets. Tool calls are executed serially, as the official orchestrator does.

Every arm that differs from the SAC bundle defaults in a few config keys links a *derived copy* of
the plugin (``work/bench/plugin_versions/<name>/dsh-sac-compact``) whose own ``cordis.patch.yml``
carries the complete config, because a profile patch's ``config:`` replaces the bundle's config
instead of merging into it.

Existing profiles are never touched; a regenerated profile is overwritten in place.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DSH_HOME = REPO / "work" / "dshhome"
BRIDGE = REPO / "plugins" / "dsh-tau2-bridge"
SAC = REPO / "plugins" / "dsh-sac-compact"
# The plugin as it was before the multi-turn fix: `git archive e01ac12 plugins/dsh-sac-compact`.
OLD_SAC = REPO / "work" / "bench" / "plugin_versions" / "old-1.1.0-e01ac12" / "plugins" / "dsh-sac-compact"
DERIVED = REPO / "work" / "bench" / "plugin_versions"
# The paper's EoCC setting for τ²-bench is written "total context limit / compaction threshold"
# (16K/12K, 12K/8K, 8K/6K, Table 8). Only the threshold matters here: the fold trigger.
EOCC_THRESHOLD_TOKENS = 8000
# The files package.json's "files" list: everything the runtime loads from the plugin.
PLUGIN_FILES = ("index.js", "lib/sac-policy.js", "cordis.patch.yml", "package.json", "README.md")


def eocc_overrides(threshold: int = EOCC_THRESHOLD_TOKENS) -> dict:
    """Config lines that turn the 1.2.0 plugin into the paper's EoCC (§2.4.1).

    ``pinInstructions: false`` restores the single anchor (the first user message is the original
    task and stays verbatim). ``minSpanChars`` is unreachable, so nothing folds by action count;
    ``contextBudgetTokens`` is the compaction threshold, and the forced fold takes everything after
    the anchor (no raw last observation, no trailing reply kept), i.e. a fresh conversation
    ``system + original task + compacted state``.
    """
    return {
        "pinInstructions": "false",
        "actionsPerCompaction": "1",
        "minSpanChars": "10000000",
        "minSpanTokens": "0",
        "contextBudgetTokens": str(int(threshold)),
        "keepLastObservation": "false",
        "keepTrailingReply": "false",
    }


EOCC_NOTE = (
    "EoCC arm (the paper's End-of-Conversation Compaction, arXiv:2611.05306 §2.4.1) on the SAC plugin.\n"
    "# The code files are byte-identical to plugins/dsh-sac-compact 1.2.0; only this bundle config differs:\n"
    "# nothing folds by action count, and once the surface reaches `contextBudgetTokens` (the compaction\n"
    "# threshold, the paper's 12K/8K setting for τ²-bench) everything after the first user message is folded\n"
    "# into one bounded recap, so the next request is system + original task + compacted state."
)


def _rewrite_patch(text: str, overrides: dict, note: str) -> str:
    """Replace (or append) top-level ``key: value`` lines of the plugin's bundle config."""
    lines = text.splitlines()
    done: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = re.match(r"^([ \t]+)([A-Za-z][A-Za-z0-9]*): .*$", line)
        if match is None or match.group(2) not in overrides:
            continue
        key = match.group(2)
        if key in done:
            raise SystemExit(f"cordis.patch.yml: key {key!r} appears more than once")
        lines[index] = f"{match.group(1)}{key}: {overrides[key]}"
        done[key] = index
    missing = [key for key in overrides if key not in done]
    if missing:
        # Append after the last `key: value` line of the file, with its indentation.
        last = max(index for index, line in enumerate(lines) if re.match(r"^[ \t]+[A-Za-z][A-Za-z0-9]*: ", line))
        indent = re.match(r"^([ \t]+)", lines[last]).group(1)
        for offset, key in enumerate(missing, start=1):
            lines.insert(last + offset, f"{indent}{key}: {overrides[key]}")
    return "\n".join(f"# {row}" if not row.startswith("# ") else row for row in note.splitlines()) + "\n" + "\n".join(lines) + "\n"


def derive_plugin(name: str, overrides: dict, note: str, source: Path = SAC, code_replacements: dict | None = None) -> Path:
    """A copy of the SAC plugin whose bundle config carries ``overrides``.

    A profile patch's ``config:`` *replaces* the bundle's config instead of merging into it, so an
    arm that differs from the bundle defaults in a few keys cannot be written as a profile override
    without silently dropping every other key. Such an arm therefore links a copy of the plugin whose
    own ``cordis.patch.yml`` carries the complete config; the code files stay byte-identical to
    ``source``. Regenerating is idempotent.

    ``code_replacements`` (optional) applies exact, unique substring replacements to ``index.js``
    — used only to give an arm the paper's own compaction wording instead of 1.2.0's anchored one.
    """
    target = DERIVED / name / "dsh-sac-compact"
    (target / "lib").mkdir(parents=True, exist_ok=True)
    for rel in PLUGIN_FILES:
        if rel not in ("cordis.patch.yml", "index.js"):
            shutil.copyfile(source / rel, target / rel)
    text = (source / "index.js").read_text(encoding="utf-8")
    for old, new in (code_replacements or {}).items():
        if text.count(old) != 1:
            raise SystemExit(f"derive_plugin({name}): index.js has {text.count(old)} occurrences of {old[:70]!r}, expected exactly one")
        text = text.replace(old, new)
    (target / "index.js").write_text(text, encoding="utf-8")
    patch = _rewrite_patch((source / "cordis.patch.yml").read_text(encoding="utf-8"), overrides, note)
    (target / "cordis.patch.yml").write_text(patch, encoding="utf-8")
    return target


def arm_specs(eocc_threshold: int = EOCC_THRESHOLD_TOKENS, sac_path: Path = SAC) -> dict:
    """The comparison arms: profile name → ``write_profile`` keyword arguments."""
    anchored = derive_plugin(f"eocc-1.2.0-thr{int(eocc_threshold)}", eocc_overrides(eocc_threshold), EOCC_NOTE, source=sac_path)
    paper = derive_plugin(
        f"eocc-paper-1.2.0-thr{int(eocc_threshold)}", eocc_overrides(eocc_threshold), EOCC_PAPER_NOTE, source=sac_path,
        code_replacements=EOCC_PAPER_WORDING,
    )
    return {
        "tau2-sac": dict(sac=True, sac_path=sac_path, arm="sac"),
        "tau2-fha": dict(sac=False, sac_path=sac_path, arm="fha"),
        "tau2-sac-old": dict(sac=True, sac_path=OLD_SAC, arm="sac-old"),
        # The EoCC arm of the comparison: the paper's regime with the paper's own compaction wording.
        "tau2-eocc": dict(sac=True, sac_path=paper, arm="eocc"),
        # Reference only: the same regime writing its state with plugin 1.2.0's anchored template.
        "tau2-eocc-anchored": dict(sac=True, sac_path=anchored, arm="eocc-anchored"),
    }


EOCC_PAPER_NOTE = (
    "EoCC arm (the paper's End-of-Conversation Compaction, arXiv:2611.05306 §2.4.1) on the SAC plugin.\n"
    "# Bundle config: nothing folds by action count; once the surface reaches `contextBudgetTokens` (the\n"
    "# compaction threshold, the paper's 12K/8K setting for τ²-bench) everything after the first user message\n"
    "# is folded into one bounded recap, so the next request is system + original task + compacted state.\n"
    "# index.js carries one documented change: the compaction instruction is the paper's own Appendix G.1\n"
    "# wording ('The conversation will reset to the system prompt, task, and this recap.' / 'Objective - one\n"
    "# line describing the required result.') instead of 1.2.0's wording anchored on the first message,\n"
    "# which would tell the summarizer to describe that first message even after the user moved on."
)

# Exact substrings of index.js (plugin 1.2.0) → the paper's Appendix G.1 compaction wording.
EOCC_PAPER_WORDING = {
    "'The first user message above is the request this work belongs to. The conversation will reset'\n"
    "        + ' to the system prompt, that request, and this recap.'":
        "'The conversation will reset to the system prompt, task, and this recap.'",
    "    lines.push(\n"
    "      '1. Objective - one line: the result the user asks for in that first message. Take it from',\n"
    "      '   the message itself, never from whatever the last actions happened to be doing;',\n"
    "      '   sub-tasks belong in the plan below, not here.',\n"
    "    )":
        "    lines.push('1. Objective - one line describing the required result.')",
}

# Rows disabled in both arms. Only model-facing tools and services that would
# add prompt text or model calls are listed; infrastructure rows stay mounted.
# (`plan-mode` cannot be disabled by a patch in this DSH build and is inert
# unless the session is switched to plan mode; the offline self-test checks that
# its section does not reach the model.)
HERMETIC_ROWS = [
    "tool-bash",
    "tool-pwsh",
    "tool-jobs",
    "tool-working-directory",
    "tool-fs",
    "tool-fs-search",
    "tool-skill",
    "skill",
    "skill-filesystem",
    "agent-instructions",
    "tool-todo",
    "goal",
    "goal-round-driver",
    "command-goal",
    "tool-goal",
    "user-questions",
    "session-title-llm",
    "web",
    "web-search-deepseek",
    "web-deepseek-api",
    "web-fetch-http",
    "tool-web",
    "mcp-resources",
    "tool-subagent",
    "tool-subagent-fork",
    "tool-subagent-control",
    "tool-workflow",
    "workflow-ptc",
    "tool-agent-team",
]

CORDIS_YML = (
    "# dsh profile root — an empty entry list. The tree is composed as patches:\n"
    "# each bundle in package.json's dsh.profile.bundles, then cordis.patch.yml, then any\n"
    "# --patch overlays. Edit cordis.patch.yml, not this file.\n"
    "[]\n"
)
PNPM_WORKSPACE = "packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n"


def q(value) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(str(value), ensure_ascii=False)


def _source_digest(*paths: Path) -> dict:
    out = {}
    for path in paths:
        out[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return out


def write_profile(
    name: str,
    *,
    dsh_home: Path = DEFAULT_DSH_HOME,
    sac: bool = True,
    sac_path: Path = SAC,
    provider: str = "jms-proxy",
    model: str = "GLM-5.3-Inner",
    base_url: str = os.environ.get("SAC_BASE_URL", "https://api.openai.com/v1"),
    api_key_env: str = "JMS_PROXY_API_KEY",
    max_tokens: int = 16384,
    display_name: str | None = None,
    extra_disabled: tuple[str, ...] = (),
    patch_text: str = "",
    arm: str | None = None,
) -> Path:
    """Write one profile and return its directory."""
    if not name or any(ch in name for ch in "/\\") or name in (".", ".."):
        raise ValueError(f"bad profile name: {name!r}")
    profile = dsh_home / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    bundles = ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless", "@local/dsh-tau2-bridge"]
    dependencies = {"@local/dsh-tau2-bridge": f"link:{BRIDGE}"}
    if sac:
        bundles.append("@local/dsh-sac-compact")
        dependencies["@local/dsh-sac-compact"] = f"link:{sac_path}"
    (profile / "package.json").write_text(
        json.dumps(
            {
                "name": f"dsh-profile-{name}",
                "private": True,
                "dsh": {"profile": {"bundles": bundles}},
                "dependencies": dependencies,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (profile / "cordis.yml").write_text(CORDIS_YML, encoding="utf-8")
    (profile / "pnpm-workspace.yaml").write_text(PNPM_WORKSPACE, encoding="utf-8")
    links = profile / "node_modules" / "@local"
    links.mkdir(parents=True, exist_ok=True)
    for package, target in dependencies.items():
        # `@local/dsh-sac-compact` links to node_modules/@local/dsh-sac-compact
        link = links / package.removeprefix("@local/")
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(Path(target.removeprefix("link:")).resolve())

    lines = [
        "# Generated by harness/tau2/make_profiles.py — edit that script, not this file.",
        f"# arm: {arm or ('sac' if sac else 'fha')} | provider: {provider} | model: {model}",
        "# The SAC arm's config is the one in the linked plugin's own cordis.patch.yml: a `config:`",
        "# here would replace that whole config, not merge into it (see make_profiles.derive_plugin).",
        "",
        "- id: agent-default-model",
        "  name: '@deepseek-ai/dsh-agent-default-model'",
        "  config:",
        f"    provider: {q(provider)}",
        f"    model: {q(model)}",
        "- id: llm-pi-ai",
        "  name: '@deepseek-ai/dsh-llm-pi-ai'",
        "  config:",
        "    providers:",
        f"      {q(provider)}:",
        f"        displayName: {q(display_name or provider)}",
        f"        apiKeyEnv: {q(api_key_env)}",
        "        api: openai-completions",
        f"        baseURL: {q(base_url)}",
        "        models:",
        f"          - id: {q(model)}",
        f"            name: {q(model)}",
        f"            maxTokens: {int(max_tokens)}",
        "",
        "# A customer-service agent, not a coding agent.",
        "- id: system-prompt",
        "  config:",
        "    personaPrefix: You are a customer service agent powered by the {{model}} model.",
        "",
        "# The official orchestrator executes a tool-call message one call at a time, in order.",
        "- id: agent-loop",
        "  config:",
        "    maxParallelToolCalls: 1",
    ]
    lines += ["", "# Hermetic: the model sees the tau2 policy and exactly the tau2 domain tools."]
    for row in list(HERMETIC_ROWS) + list(extra_disabled):
        lines += [f"- id: {row}", "  disabled: true"]
    if patch_text:
        lines += ["", patch_text.rstrip()]
    (profile / "cordis.patch.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    info = {
        "arm": arm or ("sac" if sac else "fha"),
        "provider": provider,
        "model": model,
        "sac_path": str(sac_path.resolve()) if sac else None,
        "sac_version": json.loads((sac_path / "package.json").read_text(encoding="utf-8")).get("version") if sac else None,
        "sac_source_sha256_16": _source_digest(sac_path / "index.js", sac_path / "lib" / "sac-policy.js", sac_path / "cordis.patch.yml") if sac else None,
        "bridge_source_sha256_16": _source_digest(BRIDGE / "index.js"),
        "repo_head": _git_head(),
    }
    (profile / "PROFILE_INFO.json").write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    return profile


def _git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dsh-home", default=os.environ.get("TAU2_DSH_HOME") or str(DEFAULT_DSH_HOME))
    parser.add_argument("--sac-path", default=str(SAC), help="dsh-sac-compact package the SAC arm links to")
    parser.add_argument("--provider", default="jms-proxy")
    parser.add_argument("--model", default="GLM-5.3-Inner")
    parser.add_argument("--base-url", default=os.environ.get("SAC_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key-env", default="JMS_PROXY_API_KEY")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--only", nargs="*", default=None, help="subset of the arms (default: all four)")
    parser.add_argument("--with-pin1", action="store_true", help="also write tau2-sac-pin1 (SAC with maxPinnedInstructions 1, the lean setting)")
    parser.add_argument("--eocc-threshold", type=int, default=int(os.environ.get("TAU2_EOCC_THRESHOLD") or EOCC_THRESHOLD_TOKENS),
                        help=f"EoCC compaction threshold in estimated tokens (default {EOCC_THRESHOLD_TOKENS}, the paper's 12K/8K)")
    args = parser.parse_args()

    dsh_home = Path(args.dsh_home).resolve()
    if not (dsh_home / "profiles").is_dir():
        raise SystemExit(f"{dsh_home} is not a DSH home with a profiles/ directory")
    common = dict(
        dsh_home=dsh_home,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        max_tokens=args.max_tokens,
        display_name=args.display_name,
    )
    # --sac-path is the source of every arm built on the current plugin code (tau2-sac, and the
    # derived EoCC / pin1 copies).
    source = Path(args.sac_path).resolve()
    specs = arm_specs(args.eocc_threshold, sac_path=source)
    pin1_note = (
        "SAC 1.2.0 with maxPinnedInstructions 1 (the lean setting: only the newest request stays\n"
        "# verbatim, earlier ones merge into one background state). Code files identical to plugins/dsh-sac-compact."
    )
    pin1 = derive_plugin("pin1-1.2.0", {"maxPinnedInstructions": "1"}, pin1_note, source=source)
    wanted = args.only or list(specs) + (["tau2-sac-pin1"] if args.with_pin1 else [])
    for name in wanted:
        if name == "tau2-sac-pin1":
            spec = dict(sac=True, sac_path=pin1, arm="sac-pin1")
        elif name in specs:
            spec = dict(specs[name])
        else:
            raise SystemExit(f"unknown profile {name!r}; choose from {sorted(specs)} or tau2-sac-pin1")
        profile = write_profile(name, **spec, **common)
        print(f"wrote {profile}")
    print("check with: DSH_HOME=<dsh home> dsh --profile tau2-sac --dump-config | grep -n 'id: '")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
