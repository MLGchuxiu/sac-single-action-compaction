"""The SAC / EoCC / FHA execution loop.

One ``Runner`` instance executes one (task, model, regime) cell of the test
matrix. It enforces the hard request budget (130,172 input tokens, 8,192 output
tokens), records every request's provider-reported usage, and produces a
``RunResult`` the report is generated from.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import Client, Completion, LLMError, Usage
from .prompts import (
    CHECKLIST_PLAN,
    SYSTEM_PROMPT_INLINE,
    EoCC_INSTRUCTION,
    INITIAL_STATE,
    INLINE_STATE_HINT,
    STATE_NUDGE,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_COMPACT,
    build_task_message,
    state_instruction,
)
from .tokens import TokenMeter, estimate_text
from .tools import TOOL_SCHEMAS, Sandbox, dispatch

INPUT_BUDGET = 130_172
OUTPUT_BUDGET = 8_192


@dataclass
class RunResult:
    task_id: str
    model: str
    regime: str
    state_version: str = ""
    ok: bool = False
    score: float = 0.0
    detail: str = ""
    answer: str = ""
    actions: int = 0
    requests: int = 0
    compactions: int = 0
    usage: Usage = field(default_factory=Usage)
    peak_prompt_tokens: int = 0
    peak_request_tokens: int = 0
    total_prompt_tokens: int = 0
    state_tokens: list[int] = field(default_factory=list)
    state_calls: int = 0
    inline_hits: int = 0
    inline_misses: int = 0
    cache_hits: int = 0
    gated_steps: int = 0
    budget_violations: int = 0
    emergency_trims: int = 0
    errors: int = 0
    wall_s: float = 0.0
    ledger: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "model": self.model,
            "regime": self.regime,
            "state_version": self.state_version,
            "ok": self.ok,
            "score": round(self.score, 4),
            "detail": self.detail,
            "answer": self.answer[:4000],
            "actions": self.actions,
            "requests": self.requests,
            "compactions": self.compactions,
            "usage": self.usage.as_dict(),
            "peak_prompt_tokens": self.peak_prompt_tokens,
            "peak_request_tokens": self.peak_request_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "state_tokens": self.state_tokens,
            "state_calls": self.state_calls,
            "inline_hits": self.inline_hits,
            "inline_misses": self.inline_misses,
            "cache_hits": self.cache_hits,
            "gated_steps": self.gated_steps,
            "budget_violations": self.budget_violations,
            "emergency_trims": self.emergency_trims,
            "errors": self.errors,
            "wall_s": round(self.wall_s, 2),
            "ledger": self.ledger,
            "trace": self.trace,
        }


class TranscriptArchive:
    """Every message ever produced, searchable — the paper's selective history."""

    def __init__(self) -> None:
        self.entries: list[tuple[int, str, str]] = []

    def add(self, step: int, role: str, text: str) -> None:
        if text.strip():
            self.entries.append((step, role, text))

    def search(self, query: str, limit: int = 8) -> str:
        terms = [term for term in re.split(r"\s+", query.strip()) if term]
        if not terms:
            return "(empty query)"
        scored: list[tuple[int, int, str]] = []
        for step, role, text in self.entries:
            lowered = text.lower()
            score = sum(lowered.count(term.lower()) for term in terms)
            if score == 0:
                continue
            position = min(lowered.find(term.lower()) for term in terms if term.lower() in lowered)
            start = max(0, position - 240)
            snippet = text[start:start + 900].replace("\n", " ")
            scored.append((score, step, f"[step {step} · {role}] {snippet}"))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            return "(no archived match)"
        return "\n\n".join(item[2] for item in scored[:limit])


FEATURES: dict[str, set[str]] = {
    "v1": set(),
    "v2": set(),
    "v3": {"digest"},
    "v4": {"digest", "ledger", "recall", "adaptive"},
    "v5": {"digest", "ledger", "recall", "adaptive", "checklist"},
    "v6": {"digest", "ledger", "recall", "adaptive", "checklist", "inline"},
    "v7": {"digest", "ledger", "recall", "adaptive", "checklist", "inline", "readcache"},
    "v8": {"digest", "ledger", "recall", "adaptive", "checklist", "inline", "readcache", "finish_guard"},
    # v9 = the recommended combination: v6 pipeline + full command ledger, no guard, no read cache.
    "v9": {"digest", "ledger", "recall", "adaptive", "checklist", "inline", "full_ledger"},
    # v10 = v9 + observation ledger (images, repeats) + image pinning + repeat guard.
    "v10": {"digest", "ledger", "recall", "adaptive", "checklist", "inline", "full_ledger",
            "image_pin", "repeat_guard"},
    # v11 = v10 + evidence-first structured state + note registry + finish gate.
    "v11": {"digest", "ledger", "recall", "adaptive", "checklist", "inline", "full_ledger",
            "image_pin", "repeat_guard", "structured_state", "notes", "finish_gate"},
    # v12 = v11 + metric provenance + contradiction guard.
    "v12": {"digest", "ledger", "recall", "adaptive", "checklist", "inline", "full_ledger",
            "image_pin", "repeat_guard", "structured_state", "notes", "finish_gate",
            "metric_provenance", "contradiction_guard"},
    # v13 = v12 + two gates that target the observed failure directly:
    #   fresh_evidence_gate  - a visual conclusion must be made while the pixels are in context
    #   cross_method_required - a number produced by a detector needs an independent method
    "v13": {"digest", "ledger", "recall", "adaptive", "checklist", "inline", "full_ledger",
            "image_pin", "repeat_guard", "structured_state", "notes", "finish_gate",
            "metric_provenance", "contradiction_guard", "fresh_evidence_gate", "cross_method_required"},
}

CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "check",
        "description": "Mark one checklist item as completed.",
        "parameters": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "1-based checklist index."}},
            "required": ["index"],
        },
    },
}

NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "note",
        "description": (
            "Register one verified claim together with the observation that supports it. "
            "Every claim in your final answer must be registered here first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "What the claim is about, e.g. 'item_count'."},
                "value": {"type": "string", "description": "The claimed value, e.g. '6'."},
                "evidence": {"type": "string", "description": "Which observation supports it and what it showed."},
                "method": {"type": "string", "description": "How it was obtained: visual | script | read | ..."},
            },
            "required": ["subject", "value", "evidence"],
        },
    },
}

STATE_BLOCK = re.compile(r"<state>(.*?)</state>", re.S | re.I)

# Filenames that are renderings of a detector's output rather than the page itself.
DERIVED_IMAGE = re.compile(r"overlay|mask|annot|polar|unwrap|red|hough|grid|debug|test_", re.I)


def extract_state_block(text: str) -> tuple[str | None, str]:
    """Pull an inline ``<state>`` block out of an assistant reply."""
    match = STATE_BLOCK.search(text or "")
    if match is None:
        return None, text
    body = match.group(1).strip()
    cleaned = STATE_BLOCK.sub("", text).strip()
    return (body or None), cleaned


RECALL_TOOL = {
    "type": "function",
    "function": {
        "name": "recall",
        "description": "Search the full archived transcript of this task (every earlier message and tool output) and return matching snippets.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def split_observation(observation: Any) -> tuple[str, dict | None]:
    """Keep tool messages text-only; images travel in a following user message.

    OpenAI-compatible providers reject (or silently drop) image parts inside a
    `role: tool` message, which shows up as a closed connection mid-run.
    """
    if isinstance(observation, str):
        return observation, None
    if not isinstance(observation, list):
        return _text_of(observation), None
    texts = [part.get("text", "") for part in observation
             if isinstance(part, dict) and part.get("type") == "text"]
    images = [part for part in observation
              if isinstance(part, dict) and part.get("type") in ("image", "image_url")]
    text = "\n".join(item for item in texts if item) or "[image observation]"
    if not images:
        return text, None
    return text, {"role": "user", "content": images}


class RunLedger:
    """Machine-maintained facts about the run.

    The model-written state is lossy; this ledger is not. It is prepended to the
    semantic state so paths already read, files already edited, images already
    viewed and repeated calls survive every compaction regardless of model
    behavior (v10+: images and duplicate detection included).
    """

    def __init__(self, *, images: bool = False, repeats: bool = False) -> None:
        self.reads: dict[str, str] = {}
        self.edits: dict[str, int] = {}
        self.writes: dict[str, int] = {}
        self.bash: list[str] = []
        self.last_result: str = ""
        self.track_images = images
        self.track_repeats = repeats
        self.images: list[tuple[str, int, int]] = []          # (path, bytes, step)
        self.seen_calls: dict[str, tuple[int, str]] = {}      # key -> (step, digest)
        self.repeat_steps: list[int] = []

    @staticmethod
    def call_key(name: str, arguments: dict) -> str:
        try:
            blob = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = str(arguments)
        return f"{name}|{blob}"

    def already_seen(self, name: str, arguments: dict) -> tuple[int, str] | None:
        """Return (first_step, digest) when this exact call was made before."""
        if not self.track_repeats:
            return None
        return self.seen_calls.get(self.call_key(name, arguments))

    def observe(self, name: str, arguments: dict, observation: str, step: int = 0) -> None:
        path = str(arguments.get("path", "")).strip()
        if name == "read_file" and path:
            self.reads[path] = f"{arguments.get('offset', 1)}+{arguments.get('limit', 200)}"
        elif name == "edit_file" and path:
            self.edits[path] = self.edits.get(path, 0) + 1
        elif name == "write_file" and path:
            self.writes[path] = self.writes.get(path, 0) + 1
        elif name == "bash":
            command = " ".join(str(arguments.get("command", "")).split())[:160]
            if not self.bash or self.bash[-1] != command:
                self.bash = (self.bash + [command])[-40:]
            tail = " ".join(observation.split())[-260:]
            self.last_result = f"{command} -> {tail}"
        if self.track_images and name == "view_image" and path:
            size = 0
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
            self.images = (self.images + [(path, size, step)])[-24:]
        if self.track_repeats:
            key = self.call_key(name, arguments)
            if key not in self.seen_calls:
                digest = " ".join(observation.split())[:200] or "(empty)"
                self.seen_calls[key] = (step, digest)

    def render(self, limit_chars: int = 1600, full_commands: bool = False) -> str:
        parts = ["LEDGER (machine-maintained, exact; trust it over memory)"]
        commands = self.bash if full_commands else self.bash[-4:]
        if commands:
            label = "commands_run (oldest to newest)" if full_commands else "recent_commands"
            parts.append(f"{label}: " + " | ".join(commands))
        if self.reads:
            items = ", ".join(f"{path}({span})" for path, span in list(self.reads.items())[-12:])
            parts.append(f"files_read: {items}")
        if self.edits or self.writes:
            changed = [f"{path} x{count}" for path, count in {**self.writes, **self.edits}.items()]
            parts.append("files_changed: " + ", ".join(changed[-10:]))
        if self.images:
            viewed = ", ".join(f"{Path(p).name}({b // 1024}KB, step {s})" for p, b, s in self.images[-10:])
            parts.append(f"images_viewed (raw pixels are gone; re-view only if you must): {viewed}")
        if self.repeat_steps:
            parts.append(f"repeated_calls: {len(self.repeat_steps)} (steps {self.repeat_steps[-8:]}); "
                         "do not re-run an action that is already listed above")
        if self.last_result:
            parts.append("last_observation: " + self.last_result)
        text = "\n".join(parts)
        return text[:limit_chars]

    def as_dict(self) -> dict:
        return {"reads": len(self.reads), "edits": sum(self.edits.values()),
                "writes": sum(self.writes.values()), "commands": len(self.bash),
                "images": len(self.images), "repeats": len(self.repeat_steps)}


class Runner:
    """Execute one cell of the matrix."""

    def __init__(self, client: Client, meter: TokenMeter, *, model: str, regime: str,
                 state_version: str = "v1", input_budget: int = INPUT_BUDGET,
                 output_budget: int = OUTPUT_BUDGET, max_actions: int = 24,
                 eocc_threshold: int = 96_000, state_budget: int = 2048,
                 verbose: bool = False):
        self.client = client
        self.meter = meter
        self.model = model
        self.regime = regime
        self.state_version = state_version
        self.input_budget = input_budget
        self.output_budget = output_budget
        self.max_actions = max_actions
        self.eocc_threshold = eocc_threshold
        self.state_budget = state_budget
        self.gate_tokens = 250
        self._guard_finish = False
        self.verbose = verbose
        self._ledger: RunLedger | None = None
        self._finish_blocks = 0
        self._image_pin = False
        self._repeat_guard = False
        self._notes_enabled = False
        self._finish_gate = False
        self._contradiction_guard = False
        self._metric_provenance = False
        self._fresh_evidence = False
        self._cross_method = False
        self._notes: list[dict] = []
        self._gate_blocks = 0
        self._nudges = 0

    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    def _state_budget(self, actions: int) -> int:
        """Adaptive state budget (iteration 4): tighter early, stable later."""
        if self.state_version not in ("v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"):
            return self.state_budget
        return max(512, min(self.state_budget, 640 + 48 * actions))

    def _enforce_budget(self, messages: list[dict], tools: list[dict]) -> tuple[list[dict], int, int]:
        """Keep the request inside the hard input budget.

        Returns (messages, violations, trims). A violation means the untrimmed
        request would have exceeded the limit; the trim itself is what keeps the
        provider request legal.
        """
        estimate = self.meter.estimate(self.model, messages, tools)
        if estimate <= self.input_budget:
            return messages, 0, 0
        violations, trims = 1, 0
        working = list(messages)
        # 1) Shrink oversized observations, oldest first: keeps tool pairing valid.
        for index in range(2, len(working)):
            if self.meter.estimate(self.model, working, tools) <= self.input_budget:
                break
            text = _text_of(working[index].get("content"))
            if len(text) > 3000:
                working[index] = {**working[index], "content": text[:3000] + "\n[trimmed to fit the request budget]"}
                trims += 1
        # 2) Drop the oldest complete assistant/tool group.
        while self.meter.estimate(self.model, working, tools) > self.input_budget and len(working) > 4:
            drop_end = None
            for index in range(2, len(working)):
                if working[index].get("role") == "assistant":
                    drop_end = index
                    while drop_end + 1 < len(working) and working[drop_end + 1].get("role") == "tool":
                        drop_end += 1
                    break
            if drop_end is None or drop_end < 2:
                break
            del working[2:drop_end + 1]
            trims += 1
        return working, violations, trims

    # ------------------------------------------------------------------ #
    def _make_checklist(self, task) -> list[str]:
        """One planning call: the subgoals this task must satisfy."""
        try:
            completion = self.client.complete_text(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT_COMPACT},
                    {"role": "user", "content": f"<task>\n{task.prompt}\n</task>\n\n{CHECKLIST_PLAN}"},
                ],
                max_tokens=400, purpose="checklist",
            )
        except LLMError:
            return []
        items = []
        for line in completion.content.splitlines():
            text = line.strip().lstrip("-*0123456789.) ").strip()
            if text and len(text) > 3:
                items.append(text[:160])
        return items[:8]

    def _render_checklist(self, checklist: list[str], done: set[int]) -> str:
        if not checklist:
            return ""
        lines = ["CHECKLIST (maintained by the harness; call check(index) when an item is done):"]
        for index, item in enumerate(checklist, 1):
            mark = "x" if index in done else " "
            lines.append(f"[{mark}] {index}. {item}")
        return "\n".join(lines)

    def _task_block(self, task, state: str, checklist: list[str], done: set[int],
                    inline: bool) -> str:
        body = build_task_message(task.prompt, task.input_details, state)
        extra = self._render_checklist(checklist, done)
        if extra:
            body = f"{body}\n\n{extra}"
        if inline:
            body = f"{body}\n\n{INLINE_STATE_HINT}"
        if self._notes_enabled:
            body = (f"{body}\n\nEVIDENCE REGISTRY: before you call finish, register every claim with "
                    "note(subject, value, evidence, method). finish is refused while a claim has no evidence "
                    "or two claims contradict each other.")
        if self._image_pin:
            body = (f"{body}\n\nOBSERVATION RULE: images are evidence. After viewing one, write what it showed "
                    "(counts, labels, positions) into IMAGES_OBSERVED in your state; the pixels will not survive.")
        return body

    def run(self, task) -> RunResult:
        started = time.monotonic()
        root = Path(task.workdir)
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        spill = root / ".spill"
        spill.mkdir(exist_ok=True)
        features = FEATURES.get(self.state_version, set()) if self.regime == "sac" else set()
        self._guard_finish = "finish_guard" in features
        self._full_ledger = "full_ledger" in features or "finish_guard" in features
        self._ledger_chars = 2400 if self._full_ledger else 1600
        self._image_pin = "image_pin" in features
        self._repeat_guard = "repeat_guard" in features
        self._notes_enabled = "notes" in features
        self._finish_gate = "finish_gate" in features
        self._contradiction_guard = "contradiction_guard" in features
        self._metric_provenance = "metric_provenance" in features
        self._fresh_evidence = "fresh_evidence_gate" in features
        self._cross_method = "cross_method_required" in features
        self._notes = []
        self._gate_blocks = 0
        sandbox = Sandbox(root=root, spill_dir=spill, read_cache="readcache" in features)
        task.setup(root)

        result = RunResult(task_id=task.id, model=self.model, regime=self.regime,
                           state_version=self.state_version if self.regime == "sac" else "")
        archive = TranscriptArchive()
        ledger = RunLedger(images=self._image_pin, repeats=self._repeat_guard)
        self._ledger = ledger
        tools = list(TOOL_SCHEMAS)
        if "recall" in features:
            tools.append(RECALL_TOOL)
        if "checklist" in features:
            tools.append(CHECK_TOOL)
        if self._notes_enabled:
            tools.append(NOTE_TOOL)

        checklist: list[str] = []
        done_items: set[int] = set()
        if "checklist" in features:
            checklist = self._make_checklist(task)
            result.state_calls += 1

        state = INITIAL_STATE
        pending: list[dict] = []
        history: list[dict] = []
        system = SYSTEM_PROMPT_COMPACT if self.regime == "sac" else SYSTEM_PROMPT
        if "inline" in features:
            system = SYSTEM_PROMPT_INLINE
        task_message = build_task_message(task.prompt, task.input_details,
                                          state if self.regime == "sac" else None)
        archive.add(0, "task", task.prompt)

        answer = ""
        finished = False
        step = 0
        while step < self.max_actions:
            step += 1
            inline_state: str | None = None
            if self.regime == "sac":
                block = self._task_block(task, state, checklist, done_items, "inline" in features)
                messages = [{"role": "system", "content": system},
                            {"role": "user", "content": block}] + pending
            else:
                messages = [{"role": "system", "content": system},
                            {"role": "user", "content": task_message}] + history
                if self.regime == "eocc":
                    if self.meter.estimate(self.model, messages, tools) > self.eocc_threshold:
                        state = self._compact_eocc(messages, result)
                        history = []
                        messages = [{"role": "system", "content": system},
                                    {"role": "user", "content": build_task_message(
                                        task.prompt, task.input_details, state)}]

            messages, violations, trims = self._enforce_budget(messages, tools)
            result.budget_violations += violations
            result.emergency_trims += trims
            if violations:
                self._log(f"  ! request would exceed budget; trimmed ({trims} edits)")

            try:
                completion = self.client.complete(
                    model=self.model, messages=messages, tools=tools,
                    max_tokens=self.output_budget, purpose="action",
                )
            except LLMError as error:
                result.errors += 1
                result.detail = f"llm error: {error}"
                break

            self._record(result, completion, messages, tools, "action")
            result.actions += 1

            if "inline" in features:
                inline_state, cleaned = extract_state_block(completion.content)
                if inline_state is not None:
                    completion.content = cleaned
                    result.inline_hits += 1
                elif completion.tool_calls:
                    result.inline_misses += 1

            assistant_message = self._assistant_message(completion)
            archive.add(step, "assistant", completion.content)

            if not completion.tool_calls:
                if completion.content.strip():
                    # A reply without a tool call is usually progress narration,
                    # not a final answer: nudge like the real agent loop does and
                    # only accept it after a few attempts.
                    self._nudges += 1
                    if self._nudges <= 3:
                        if self.regime == "sac":
                            state = self._update_state(step, messages, assistant_message, [],
                                                       state, result, archive, ledger)
                            pending = [{"role": "user", "content": STATE_NUDGE}]
                        else:
                            history.append(assistant_message)
                            history.append({"role": "user", "content": STATE_NUDGE})
                        continue
                    answer = completion.content.strip()
                    finished = True
                    break
                history.append(assistant_message)
                history.append({"role": "user", "content": STATE_NUDGE})
                continue

            tool_messages: list[dict] = []
            image_messages: list[dict] = []
            finish_answer: str | None = None
            for call in completion.tool_calls:
                observation, done = self._execute(sandbox, call, archive, step,
                                                  checklist, done_items)
                text_part, image_message = split_observation(observation)
                tool_messages.append({"role": "tool", "tool_call_id": call["id"], "content": text_part})
                if image_message is not None:
                    image_messages.append(image_message)
                if done:
                    finish_answer = observation if isinstance(observation, str) else ""
            if finish_answer is not None:
                answer = finish_answer
                finished = True
                break

            tool_messages.extend(image_messages)
            if self.regime == "sac":
                step_tokens = sum(int(estimate_text(_text_of(message.get("content")))) for message in tool_messages)
                if inline_state is not None:
                    state = inline_state if "ledger" not in features else ledger.render(
                        self._ledger_chars, full_commands=self._full_ledger) + "\n\n" + inline_state
                elif "inline" in features:
                    # No state block: fall back to a dedicated call so memory is never lost.
                    state = self._update_state(step, messages, assistant_message, tool_messages,
                                               state, result, archive, ledger)
                elif "gate" in features and step_tokens < self.gate_tokens:
                    result.gated_steps += 1
                else:
                    state = self._update_state(step, messages, assistant_message, tool_messages,
                                               state, result, archive, ledger)
                if "inline" in features:
                    # Pipeline: the next reply sees this step and folds it into the state.
                    pending = [assistant_message] + tool_messages
                else:
                    pending = []
                result.state_tokens.append(int(estimate_text(state)))
            else:
                history.append(assistant_message)
                history.extend(tool_messages)

        if not finished and not answer:
            result.detail = result.detail or "action budget exhausted"

        result.answer = answer
        result.ledger = ledger.as_dict()
        result.gated_steps += self._gate_blocks
        result.cache_hits = sandbox.cache_hits
        result.ok, result.score, verify_detail = task.verify(root, answer)
        if verify_detail:
            result.detail = f"{result.detail} | {verify_detail}" if result.detail else verify_detail
        result.wall_s = time.monotonic() - started
        return result

    # ------------------------------------------------------------------ #
    def _assistant_message(self, completion: Completion) -> dict:
        message: dict[str, Any] = {"role": "assistant", "content": completion.content}
        if completion.tool_calls:
            message["tool_calls"] = [
                {"id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": call["arguments"]}}
                for call in completion.tool_calls
            ]
        return message

    def _record(self, result: RunResult, completion: Completion,
                messages: list[dict], tools: list[dict], purpose: str = "action") -> None:
        estimate = self.meter.estimate(self.model, messages, tools)
        self.meter.observe(self.model, estimate, completion.usage.prompt_tokens)
        result.requests += 1
        result.usage.add(completion.usage)
        result.total_prompt_tokens += completion.usage.prompt_tokens
        result.peak_request_tokens = max(result.peak_request_tokens, completion.usage.prompt_tokens)
        if purpose == "action":
            result.peak_prompt_tokens = max(result.peak_prompt_tokens, completion.usage.prompt_tokens)
        result.trace.append({
            "request": result.requests,
            "purpose": purpose,
            "estimated_prompt": estimate,
            "prompt_tokens": completion.usage.prompt_tokens,
            "completion_tokens": completion.usage.completion_tokens,
            "reasoning_tokens": completion.usage.reasoning_tokens,
            "latency_s": round(completion.latency_s, 2),
            "tool_calls": [call["name"] for call in completion.tool_calls],
        })

    def _conflicts(self) -> list[tuple[str, list[dict]]]:
        """Groups of notes that disagree on the same subject without a cross-method check."""
        groups: dict[str, list[dict]] = {}
        for note in self._notes:
            groups.setdefault(note["subject"].strip().lower(), []).append(note)
        conflicts = []
        for subject, items in groups.items():
            values = {item["value"].strip() for item in items}
            if len(values) <= 1:
                continue
            resolved = False
            # (a) two independent methods agree on one value, or
            # (b) the newest note uses a method that none of the older
            #     conflicting notes used - a fresh independent observation wins.
            for value in values:
                methods = {item.get("method", "").strip() for item in items
                           if item["value"].strip() == value}
                methods.discard("")
                if len(methods) >= 2:
                    resolved = True
            latest = max(items, key=lambda item: item.get("step", 0))
            older_methods = {item.get("method", "").strip() for item in items
                             if item["value"].strip() != latest["value"].strip()}
            older_methods.discard("")
            if latest.get("method", "").strip() and latest["method"].strip() not in older_methods:
                resolved = True
            if not resolved:
                conflicts.append((subject, items))
        return conflicts

    @staticmethod
    def _is_script_method(method: str) -> bool:
        text = (method or "").lower()
        return any(key in text for key in ("script", "hough", "detect", "code", "bash", "program", "python"))

    def _gate_reason(self, step: int = 0) -> str | None:
        """Why finish must be refused, or None when the evidence gate is satisfied."""
        if not self._notes:
            return ("[gate] Before finishing, register at least one verified claim with "
                    "note(subject, value, evidence, method). An answer with no registered evidence is refused.")
        missing = [note for note in self._notes if not note["evidence"].strip()]
        if missing:
            return ("[gate] These notes have no evidence: "
                    + ", ".join(note["subject"] for note in missing)
                    + ". Attach the observation that supports each one.")
        if self._contradiction_guard:
            conflicts = self._conflicts()
            if conflicts:
                lines = ["[gate] CONTRADICTION unresolved: two registered claims disagree."]
                for subject, items in conflicts:
                    detail = "; ".join(
                        f"value={item['value']} method={item.get('method') or '?'} evidence={item['evidence'][:90]}"
                        for item in items)
                    lines.append(f"  - {subject}: {detail}")
                lines.append("Resolve it with a NEW observation that is not the same method "
                             "(re-view the image, inspect the crop), then register the resolved value with note().")
                return "\n".join(lines)
        if self._cross_method:
            for note in self._notes:
                if not self._is_script_method(note.get("method", "")):
                    continue
                subject = note["subject"].strip().lower()
                independent = [
                    other for other in self._notes
                    if other["subject"].strip().lower() == subject
                    and other.get("method", "").strip().lower() != note.get("method", "").strip().lower()
                    and other["value"].strip() != note["value"].strip()
                ]
                if not independent:
                    return (f"[gate] '{note['subject']} = {note['value']}' comes only from "
                            f"{note.get('method') or 'a script'}. A detector threshold is a proxy, not an "
                            "observation: confirm the count with an independent method (view the image and "
                            "count), then register that note too.")
        if self._fresh_evidence and self._ledger is not None and self._ledger.images:
            # Only raw evidence images count: an overlay/mask/polar plot is a
            # rendering of a detector's output, not an observation of the page.
            raw = [item for item in self._ledger.images if not DERIVED_IMAGE.search(Path(item[0]).name)]
            if raw:
                last_step = raw[-1][2]
                if step - last_step > 3:
                    return (f"[gate] your last raw image observation was {step - last_step} steps ago and those "
                            "pixels are gone from your context. Re-view the key image, confirm the count, then finish.")
        return None

    def _execute(self, sandbox: Sandbox, call: dict, archive: TranscriptArchive,
                 step: int, checklist: list[str] | None = None,
                 done_items: set[int] | None = None) -> tuple[Any, bool]:
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
            observation: Any = "[error] tool arguments are not valid JSON"
            return observation, False
        name = call["name"]
        if name == "recall":
            query = str(arguments.get("query", ""))
            return archive.search(query), False
        if name == "check":
            index = int(arguments.get("index", 0) or 0)
            if done_items is not None and 1 <= index <= len(checklist or []):
                done_items.add(index)
                return f"[checked] item {index}: {checklist[index - 1]}", False
            return f"[error] checklist index {index} is out of range", False
        if name == "note":
            subject = str(arguments.get("subject", "")).strip()
            value = str(arguments.get("value", "")).strip()
            evidence = str(arguments.get("evidence", "")).strip()
            method = str(arguments.get("method", "")).strip()
            if not subject or not value or not evidence:
                return "[error] note requires subject, value and evidence", False
            self._notes.append({"subject": subject, "value": value,
                                "evidence": evidence, "method": method, "step": step})
            return (f"[noted] {subject} = {value} (method={method or 'unspecified'}); "
                    f"{len(self._notes)} claim(s) registered"), False
        if name == "finish" and self._finish_gate:
            reason = self._gate_reason(step)
            if reason is not None:
                self._gate_blocks += 1
                return reason, False

        prefix = ""
        if self._repeat_guard and self._ledger is not None:
            seen = self._ledger.already_seen(name, arguments)
            if seen is not None:
                first_step, digest = seen
                prefix = (f"[REPEAT] This exact call was already made at step {first_step}. "
                          f"Earlier result: {digest}\n---\n")
                self._ledger.repeat_steps.append(step)

        observation, done = dispatch(sandbox, name, arguments)
        text = _text_of(observation)
        if hasattr(self, "_ledger") and self._ledger is not None:
            self._ledger.observe(name, arguments, text, step)
        archive.add(step, f"tool:{name}", text)
        if prefix:
            if isinstance(observation, str):
                observation = prefix + observation
            else:
                observation = [{"type": "text", "text": prefix}] + list(observation)
        return observation, done

    def _state_block(self, state: str) -> str:
        return f"\n<semantic_state>\n{state}\n</semantic_state>"

    def _compact_eocc(self, messages: list[dict], result: RunResult) -> str:
        """End-of-conversation compaction: one recap turn, then a fresh conversation."""
        compact_messages = messages + [{"role": "user", "content": EoCC_INSTRUCTION}]
        compact_messages, violations, trims = self._enforce_budget(compact_messages, [])
        result.budget_violations += violations
        result.emergency_trims += trims
        completion = self.client.complete_text(
            model=self.model, messages=compact_messages,
            max_tokens=min(4096, self.output_budget), purpose="compaction",
        )
        if not completion.content.strip() and completion.tool_calls:
            retry = self.client.complete_text(
                model=self.model,
                messages=compact_messages + [{"role": "user",
                                              "content": "You have no tools in this turn. Respond with the recap text only."}],
                max_tokens=min(4096, self.output_budget), purpose="compaction",
            )
            retry.usage.add(completion.usage)
            completion = retry
        self._record(result, completion, compact_messages, [], "compaction")
        result.state_calls += 1
        result.compactions += 1
        text = completion.content.strip()
        return text or "(compaction produced no text)"

    def _update_state(self, step: int, messages: list[dict], assistant_message: dict,
                      tool_messages: list[dict], previous_state: str,
                      result: RunResult, archive: TranscriptArchive,
                      ledger: RunLedger | None = None) -> str:
        """Single-action compaction: fold this action into the semantic state."""
        budget = self._state_budget(step)
        update_messages = list(messages) + [assistant_message] + tool_messages
        digest_versions = ("v3", "v4", "v10", "v11", "v12")
        ledger_versions = ("v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12")
        if self.state_version in digest_versions:
            digest = self._digest(messages, assistant_message, tool_messages)
            if self.state_version in ledger_versions and ledger is not None:
                digest = ledger.render() + "\n\n" + digest
            update_messages.append({"role": "user", "content": f"ACTION DIGEST (machine-generated):\n{digest}"})
        update_messages.append({"role": "user", "content": state_instruction(self.state_version, budget)})
        update_messages, violations, trims = self._enforce_budget(update_messages, [])
        result.budget_violations += violations
        result.emergency_trims += trims
        # Long generations on the state call are what the provider drops first
        # (RemoteDisconnected); 1024 tokens is plenty for a structured state.
        import os as _os
        cap = int(_os.environ.get("SAC_STATE_MAX_TOKENS", "1024"))
        state_max_tokens = min(cap, max(768, budget))
        completion = self.client.complete_text(
            model=self.model, messages=update_messages,
            max_tokens=state_max_tokens, purpose="state",
        )
        if not completion.content.strip():
            # Reasoning-heavy models can spend the whole allowance before writing.
            retry = self.client.complete_text(
                model=self.model,
                messages=update_messages + [{
                    "role": "user",
                    "content": "Output the recap text now. Do not analyse. Start with 'OBJECTIVE:' or 'Stop work'.",
                }],
                max_tokens=4096, purpose="state",
            )
            retry.usage.add(completion.usage)
            completion = retry
        if not completion.content.strip() and completion.tool_calls:
            # The model answered with a tool call although this turn offers no tools.
            retry_messages = update_messages + [{
                "role": "user",
                "content": "You have no tools in this turn. Respond with the semantic state text only.",
            }]
            retry = self.client.complete_text(
                model=self.model, messages=retry_messages,
                max_tokens=4096, purpose="state",
            )
            retry.usage.add(completion.usage)
            completion = retry
        self._record(result, completion, update_messages, [], "state")
        result.state_calls += 1
        result.compactions += 1
        text = completion.content.strip()
        if not text:
            # Never lose the previous state when the update fails.
            return previous_state
        archive.add(step, "state", text)
        bounded = self._bound_state(text, budget)
        if self.state_version in ledger_versions and ledger is not None:
            # Hybrid state: the machine ledger cannot be forgotten, the narrative adds intent.
            bounded = ledger.render(self._ledger_chars, full_commands=self._full_ledger) + "\n\n" + bounded
        return bounded

    def _digest(self, messages: list[dict], assistant_message: dict,
                tool_messages: list[dict]) -> str:
        """Deterministic action digest: what was called and what came back."""
        lines = []
        calls = assistant_message.get("tool_calls") or []
        for call, tool_message in zip(calls, tool_messages):
            function = call.get("function", {})
            observation = _text_of(tool_message.get("content"))
            head = " ".join(observation.split())[:400]
            tail = " ".join(observation.split())[-200:]
            lines.append(f"- tool={function.get('name')} args={function.get('arguments')[:300]}")
            if function.get("name") == "view_image":
                lines.append("  IMAGE_OBSERVATION: this was a visual observation - record what it showed "
                             "(objects, counts, labels, positions) in IMAGES_OBSERVED before it leaves context")
            lines.append(f"  observation_head: {head}")
            if tail != head:
                lines.append(f"  observation_tail: {tail}")
        return "\n".join(lines) or "(no tool calls)"

    def _bound_state(self, state: str, budget: int) -> str:
        """Keep the state inside its token budget, preserving the head (objective/facts)."""
        if estimate_text(state) <= budget:
            return state
        keep = int(budget * 3.4)
        head = int(keep * 0.7)
        tail = keep - head
        return state[:head] + "\n[state truncated to budget]\n" + state[-tail:]