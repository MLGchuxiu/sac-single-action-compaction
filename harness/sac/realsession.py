"""Real-session replay: measure what each context regime would cost on sessions
that were actually run on this machine, and how much of their content survives
compaction.

Two independent measurements:

* ``structural_replay`` — no model calls. Walks the recorded message stream and
  computes, for FHA / EoCC / SAC, the exact prompt sizes that would have been
  sent (using the harness token estimator). This answers "how much context does
  a 678K-token real session actually need?"
* ``extract_facts`` + ``probe_state`` — with the model. Folds a real session
  segment into a semantic state with a given template, then asks the model, with
  *only the state in context*, to recall the exact file paths it changed and the
  commands it ran. That is the fidelity metric the paper's §3.1 warns about.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tokens import estimate_messages, estimate_text

ZSTD = "/opt/homebrew/bin/zstd"
SESSIONS_ROOT = Path(os.path.expanduser("~/.dsh/sessions"))


def decode_dirname(name: str) -> str:
    """Decode a session directory name: ``~XXXX`` escapes are UTF-16 code units."""
    def replace(match: re.Match) -> str:
        return chr(int(match.group(1), 16))
    return re.sub(r"~([0-9A-Fa-f]{4})", replace, name)


@dataclass
class Step:
    """One assistant action plus the observations it produced."""
    index: int
    assistant_text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Full prompt size: uncached input plus cache reads."""
        return self.input_tokens + self.cached_tokens

    def messages(self) -> list[dict]:
        """Render this step as plain transcript messages.

        The replay is a *summarisation input*, not a re-execution, so tool calls
        are flattened into text: that keeps it provider-agnostic (no tool-call
        pairing rules) while preserving every token the model originally saw.
        """
        rendered: list[dict] = []
        if self.assistant_text.strip():
            rendered.append({"role": "assistant", "content": self.assistant_text})
        for call, result in zip(self.calls, self.results):
            arguments = call.get("arguments") or ""
            header = f"[tool call: {call.get('name')} {arguments[:400]}]"
            rendered.append({"role": "user", "content": f"{header}\n[tool result]\n{result}"})
        return rendered


@dataclass
class RealSession:
    session_id: str
    directory: str
    cwd: str
    model: str
    turns: int
    steps: list[Step]
    system_tokens: int
    task_text: str
    total_input_tokens: int
    peak_input_tokens: int
    bytes: int

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "directory": self.directory,
            "cwd": self.cwd,
            "model": self.model,
            "turns": self.turns,
            "steps": len(self.steps),
            "total_input_tokens": self.total_input_tokens,
            "peak_input_tokens": self.peak_input_tokens,
            "bytes": self.bytes,
        }


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def load_session(path: str | Path) -> RealSession:
    """Parse one ``session.v4.jsonl.zstd`` into steps."""
    path = str(path)
    raw = subprocess.run([ZSTD, "-dc", path if path.startswith("/") else "./" + path],
                         capture_output=True, timeout=300).stdout
    if not raw:
        raise ValueError(f"cannot decompress {path}")
    meta: dict[str, Any] = {}
    task_text = ""
    steps: list[Step] = []
    current: Step | None = None
    pending_calls: list[dict] = []
    turns = 0
    model = ""
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "session":
            meta = event
        elif kind == "turn/start":
            turns += 1
        elif kind == "user/message" and not task_text:
            task_text = _block_text(data.get("content") or data.get("message", {}).get("content"))
        elif kind == "assistant/message":
            message = data.get("message") or {}
            source = message.get("source") or {}
            if source.get("model"):
                model = source["model"]
            usage = data.get("usage") or {}
            current = Step(index=len(steps),
                           assistant_text=_block_text(message.get("content")),
                           input_tokens=int(usage.get("inputTokens") or 0),
                           cached_tokens=int(usage.get("cacheReadTokens") or 0),
                           output_tokens=int(usage.get("outputTokens") or 0))
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool-call":
                    current.calls.append({"name": block.get("name") or "", "arguments": block.get("arguments") or ""})
            pending_calls = list(current.calls)
            steps.append(current)
        elif kind == "tool/result" and current is not None:
            message = data.get("message") or {}
            current.results.append(_block_text(message.get("content")))
    # Align calls with results: a step may answer only some of its calls.
    for step in steps:
        if len(step.results) < len(step.calls):
            step.calls = step.calls[:len(step.results)]
        elif len(step.results) > len(step.calls):
            step.results = step.results[:len(step.calls)]
    session_id = (meta.get("id") or Path(path).parent.name)
    directory = decode_dirname(Path(path).parts[-3]) if len(Path(path).parts) >= 3 else ""
    return RealSession(
        session_id=session_id,
        directory=directory,
        cwd=meta.get("cwd") or "",
        model=model,
        turns=turns,
        steps=steps,
        system_tokens=int(estimate_text("system prompt and tool schemas")) + 1800,
        task_text=task_text,
        total_input_tokens=sum(step.prompt_tokens for step in steps),
        peak_input_tokens=max((step.prompt_tokens for step in steps), default=0),
        bytes=os.path.getsize(path) if os.path.exists(path) else 0,
    )


def find_sessions() -> list[str]:
    """All session logs on this machine."""
    return sorted(glob.glob(str(SESSIONS_ROOT / "**" / "*.zstd"), recursive=True))


# --------------------------------------------------------------------------- #
# Structural replay (no model calls)
# --------------------------------------------------------------------------- #

def calibration_factor(session: RealSession) -> float:
    """Estimator scale that makes the FHA replay match the observed session spend.

    FHA is exactly what the recorded session did, so ``observed / estimated`` is
    the content-specific correction for this session (the harness estimator is
    calibrated globally, but real sessions mix code, CJK and tool dumps).
    """
    baseline = structural_replay(session, "fha")
    if baseline["total"] <= 0:
        return 1.0
    return session.total_input_tokens / baseline["total"]


def structural_replay(session: RealSession, regime: str, *, input_limit: int = 130_172,
                      eocc_threshold: int = 96_000, state_budget: int = 2048,
                      keep_task: bool = True, scale: float = 1.0) -> dict:
    """What would each regime have sent?

    FHA sends the whole history; EoCC resets to task+state when the estimate
    crosses the threshold; SAC sends task+state plus the single current action.
    """
    task_tokens = int(estimate_text(session.task_text[:6000])) if keep_task else 0
    base = session.system_tokens + task_tokens
    state_tokens = 0
    history: list[dict] = []
    peak = 0
    total = 0
    compactions = 0
    over_budget = 0
    for step in session.steps:
        rendered = step.messages()
        if regime == "sac":
            prompt = base + state_tokens + int(estimate_messages(rendered))
        else:
            prompt = base + int(estimate_messages(history + rendered))
            if regime == "eocc" and prompt > eocc_threshold:
                compactions += 1
                state_tokens = min(state_budget, int(prompt * 0.04))
                history = []
                prompt = base + state_tokens + int(estimate_messages(rendered))
        prompt *= scale
        if prompt > input_limit:
            over_budget += 1
        peak = max(peak, prompt)
        total += prompt
        if regime == "sac":
            state_tokens = min(state_budget, int((state_tokens + int(estimate_messages(rendered))) * 0.18) + 60)
        else:
            history = history + rendered
    return {
        "regime": regime,
        "steps": len(session.steps),
        "peak": int(peak),
        "total": int(total),
        "compactions": compactions,
        "over_budget": over_budget,
        "mean": int(total / max(1, len(session.steps))),
    }


# --------------------------------------------------------------------------- #
# Fidelity probe (model calls)
# --------------------------------------------------------------------------- #

def extract_facts(steps: list[Step]) -> dict[str, list[str]]:
    """Exact facts a handoff should preserve: changed paths and executed commands."""
    paths: list[str] = []
    commands: list[str] = []
    for step in steps:
        for call in step.calls:
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            path = str(arguments.get("path") or arguments.get("file_path") or "").strip()
            if path and call["name"] in ("write_file", "edit_file", "read_file", "create_file", "str_replace_editor"):
                paths.append(path)
            command = str(arguments.get("command") or "").strip()
            if command and call["name"] in ("bash", "shell", "run_command"):
                commands.append(" ".join(command.split())[:160])
    def dedupe(items: list[str]) -> list[str]:
        seen, out = set(), []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out
    return {"paths": dedupe(paths), "commands": dedupe(commands)}


RECALL_QUESTIONS = (
    "You are resuming work from the semantic state above. Answer from the state only; "
    "if it does not record something, write UNKNOWN for that item.\n\n"
    "1. List every file path you read or modified, one per line, exactly as written.\n"
    "2. List every shell command you ran, one per line, exactly as written.\n\n"
    "Format:\nPATHS:\n- ...\nCOMMANDS:\n- ..."
)


def parse_recall(answer: str) -> dict[str, list[str]]:
    """Split a recall answer into PATHS and COMMANDS sections.

    Tolerates markdown decoration the models actually emit: ``## 1. PATHS``,
    ``**COMMANDS:**``, numbered items, and inline explanations after an em dash.
    """
    sections = {"paths": [], "commands": []}
    mode = None
    for line in answer.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        header = upper.lstrip("#* 0123456789.):")
        if "PATH" in header[:24] and "COMMAND" not in header[:24]:
            mode = "paths"
            continue
        if "COMMAND" in header[:24]:
            mode = "commands"
            continue
        if not mode or not stripped:
            continue
        item = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", stripped)
        if item == stripped and not stripped.startswith(("-", "*", "•")):
            continue
        item = item.split(" — ")[0].split(" — ")[0].strip().strip("`").strip()
        if item and item.upper() not in ("UNKNOWN", "NONE"):
            sections[mode].append(item)
    return sections


def recall_score(recalled: list[str], expected: list[str]) -> float:
    """Fraction of expected facts that appear verbatim in the recalled list."""
    if not expected:
        return 1.0
    hits = 0
    for item in expected:
        needle = item.strip()
        if any(needle in candidate or candidate in needle for candidate in recalled if candidate):
            hits += 1
    return hits / len(expected)