"""Sandboxed filesystem + shell tools shared by every regime.

The tool surface mirrors what an agentic harness exposes: a shell, selective
file reading with offsets, exact-match editing, search, and an image viewer for
multimodal tasks. Outputs are bounded so a single observation can never blow the
input budget by itself; oversized output spills to a file the agent can re-read
selectively (the paper's "selective input reading").
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_TOOL_CHARS = 12_000
HEAD_CHARS = 8_000
TAIL_CHARS = 2_000


def _truncate(text: str, spill: Path | None = None) -> str:
    """Bound one observation, spilling the full text for later selective reads."""
    if len(text) <= MAX_TOOL_CHARS:
        return text
    if spill is not None:
        try:
            spill.parent.mkdir(parents=True, exist_ok=True)
            spill.write_text(text, encoding="utf-8")
            note = f"\n[full output: {len(text)} chars written to {spill.name}; read it selectively]"
        except OSError:
            note = ""
    else:
        note = ""
    head = text[:HEAD_CHARS]
    tail = text[-TAIL_CHARS:]
    return f"{head}\n\n[... {len(text) - HEAD_CHARS - TAIL_CHARS} chars elided ...]\n\n{tail}{note}"


@dataclass
class Sandbox:
    """One task workspace plus the counters the harness reports on."""

    root: Path
    spill_dir: Path
    read_cache: bool = False
    tool_calls: int = 0
    tool_errors: int = 0
    bytes_read: int = 0
    spilled: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    read_ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    cache_hits: int = 0

    def resolve(self, raw: str) -> Path:
        """Resolve a model-supplied path inside the sandbox, refusing escapes."""
        candidate = (self.root / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes the workspace: {raw}")
        return candidate

    def bash(self, command: str, timeout: int = 60) -> str:
        self.tool_calls += 1
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.root),
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            # Numpy/Pillow live in the DSH bundled runtime, not in /usr/bin/python3.
            "SAC_PYTHON": os.environ.get(
                "SAC_PYTHON",
                os.path.expanduser("~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3"),
            ),
        }
        try:
            completed = subprocess.run(
                ["bash", "-lc", command],
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            self.tool_errors += 1
            return f"[timeout after {timeout}s]"
        output = completed.stdout
        if completed.stderr:
            output += ("\n" if output else "") + completed.stderr
        if not output:
            output = "(no output)"
        output = f"{output}\n[exit code: {completed.returncode}]"
        self.spilled += 1
        return _truncate(output, self.spill_dir / f"spill_{self.spilled:03d}.txt")

    def read_file(self, path: str, offset: int = 1, limit: int = 200) -> str:
        self.tool_calls += 1
        if self.read_cache:
            key = f"{path}#{offset}#{limit}"
            seen = self.read_ranges.setdefault(path, [])
            if (offset, limit) in seen:
                self.cache_hits += 1
                return (f"[cache] {path} lines {offset}-{offset + limit - 1} were already read; "
                        f"the content is in your recap. Re-read only a different slice.")
            seen.append((offset, limit))
        try:
            target = self.resolve(path)
            text = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as error:
            self.tool_errors += 1
            return f"[error] {error}"
        lines = text.splitlines()
        start = max(0, int(offset) - 1)
        end = min(len(lines), start + max(1, int(limit)))
        self.bytes_read += len(text)
        body = "\n".join(f"{number + 1}\t{lines[number]}" for number in range(start, end))
        return f"[{path} lines {start + 1}-{end} of {len(lines)}]\n{_truncate(body)}"

    def write_file(self, path: str, content: str) -> str:
        self.tool_calls += 1
        try:
            target = self.resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except (OSError, ValueError) as error:
            self.tool_errors += 1
            return f"[error] {error}"
        return f"[wrote {len(content)} chars to {path}]"

    def edit_file(self, path: str, old: str, new: str) -> str:
        self.tool_calls += 1
        try:
            target = self.resolve(path)
            text = target.read_text(encoding="utf-8")
            count = text.count(old)
            if count == 0:
                self.tool_errors += 1
                return f"[error] old_string not found in {path}"
            if count > 1:
                self.tool_errors += 1
                return f"[error] old_string appears {count} times in {path}; make it unique"
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
        except (OSError, ValueError) as error:
            self.tool_errors += 1
            return f"[error] {error}"
        return f"[edited {path}]"

    def list_dir(self, path: str = ".") -> str:
        self.tool_calls += 1
        try:
            target = self.resolve(path)
            entries = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name))
        except (OSError, ValueError) as error:
            self.tool_errors += 1
            return f"[error] {error}"
        lines = [f"{'d' if entry.is_dir() else 'f'} {entry.name}" for entry in entries[:400]]
        return "\n".join(lines) or "(empty)"

    def grep(self, pattern: str, path: str = ".", max_results: int = 60) -> str:
        self.tool_calls += 1
        try:
            expression = re.compile(pattern)
            target = self.resolve(path)
        except (re.error, OSError, ValueError) as error:
            self.tool_errors += 1
            return f"[error] {error}"
        files = [target] if target.is_file() else [
            item for item in target.rglob("*")
            if item.is_file() and item.stat().st_size < 4_000_000
        ]
        hits: list[str] = []
        for file in files[:400]:
            try:
                with file.open(encoding="utf-8", errors="replace") as handle:
                    for number, line in enumerate(handle, 1):
                        if expression.search(line):
                            hits.append(f"{file.relative_to(self.root)}:{number}:{line.rstrip()[:240]}")
                            if len(hits) >= max_results:
                                break
            except OSError:
                continue
            if len(hits) >= max_results:
                break
        return _truncate("\n".join(hits) or "(no matches)")

    def view_image(self, path: str) -> list[dict[str, Any]]:
        """Return an image as a model content block (multimodal observation)."""
        self.tool_calls += 1
        try:
            target = self.resolve(path)
            data = target.read_bytes()
        except (OSError, ValueError) as error:
            self.tool_errors += 1
            return [{"type": "text", "text": f"[error] {error}"}]
        suffix = target.suffix.lower().lstrip(".") or "png"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(suffix, "png")
        return [
            {"type": "text", "text": f"[image {path} — {len(data)} bytes]"},
            {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{base64.b64encode(data).decode()}"}},
        ]


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the task workspace. Returns stdout, stderr and the exit code. Large output is truncated and spilled to a file you can read selectively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 60)."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file with line numbers. Use offset/limit to read a slice of a large file instead of dumping it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "1-based first line (default 1)."},
                    "limit": {"type": "integer", "description": "Number of lines (default 200)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact occurrence of old_string with new_string in a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}},
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries of a directory.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search files with a regular expression; prints file:line:text matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "File or directory (default '.')."},
                    "max_results": {"type": "integer"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "View an image file (png/jpeg/webp/gif) and return it to your visual context.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Submit the final answer and end the task. Call this exactly once when the task is complete.",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "string", "description": "Final answer or completion summary."}},
                "required": ["answer"],
            },
        },
    },
]


def dispatch(sandbox: Sandbox, name: str, arguments: dict[str, Any]) -> tuple[str | list[dict], bool]:
    """Execute one tool call. Returns (observation, finished)."""
    try:
        if name == "bash":
            return sandbox.bash(str(arguments.get("command", "")), int(arguments.get("timeout", 60) or 60)), False
        if name == "read_file":
            return sandbox.read_file(str(arguments.get("path", "")), int(arguments.get("offset", 1) or 1),
                                      int(arguments.get("limit", 200) or 200)), False
        if name == "write_file":
            return sandbox.write_file(str(arguments.get("path", "")), str(arguments.get("content", ""))), False
        if name == "edit_file":
            return sandbox.edit_file(str(arguments.get("path", "")), str(arguments.get("old_string", "")),
                                     str(arguments.get("new_string", ""))), False
        if name == "list_dir":
            return sandbox.list_dir(str(arguments.get("path", ".") or ".")), False
        if name == "grep":
            return sandbox.grep(str(arguments.get("pattern", "")), str(arguments.get("path", ".") or "."),
                                int(arguments.get("max_results", 60) or 60)), False
        if name == "view_image":
            return sandbox.view_image(str(arguments.get("path", ""))), False
        if name == "finish":
            return str(arguments.get("answer", "")), True
        return f"[error] unknown tool {name}", False
    except Exception as error:  # noqa: BLE001 - a tool must never crash the run
        sandbox.tool_errors += 1
        return f"[error] {type(error).__name__}: {error}", False