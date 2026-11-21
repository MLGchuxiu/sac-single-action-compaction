"""Run one benchmark task through a real `dsh headless` agent.

The agent runs inside an isolated clone of the task workspace, so arms and
repeats never share files.  Metrics come from the persisted session log
(`~/.dsh/sessions/<cwd>/<session>/session.v4.jsonl.zstd`): every request's
prompt tokens, every compaction bracket, and the state-update usage.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .common import DSH, WORK, BenchTask, clone_workspace, extract_json, git, workspace_diff

# Benchmark runs boot DSH with a workspace-local DSH_HOME so the harness does not
# write into the user's real profile/session tree (and so the run is sandboxed).
DSH_HOME = Path(os.environ.get("DSH_HOME") or (Path(__file__).resolve().parents[2]
                                               / "work" / "dshhome"))
SESSIONS = DSH_HOME / "sessions"

# arm name -> profile name. `fha` is the no-compaction baseline; any other arm
# maps to a `sac-bench[-<arm>]` profile holding that configuration.
PROFILES = {"fha": "fha-bench", "fha32": "fha-bench-32", "sac": "sac-bench",
            "default": "sac-default"}


def profile_for(arm: str) -> str:
    if arm in PROFILES:
        return PROFILES[arm]
    if arm.startswith("fha"):
        return "fha-bench"
    return f"sac-bench-{arm}"


def api_key() -> str:
    key = os.environ.get("JMS_PROXY_API_KEY")
    if key:
        return key.strip()
    text = (Path.home() / ".dsh" / ".credentials.yaml").read_text(encoding="utf-8")
    match = re.search(r"JMS_PROXY_API_KEY:\s*(\S+)", text)
    if not match:
        raise SystemExit("no JMS_PROXY_API_KEY")
    return match.group(1).strip().strip("'\"")


def session_log(session_id: str) -> Path | None:
    for path in SESSIONS.glob(f"*/{session_id}/session.v4.jsonl.zstd"):
        return path
    return None


def read_session(path: Path) -> list[dict[str, Any]]:
    result = subprocess.run(["zstd", "-dc", str(path)], capture_output=True)
    if result.returncode != 0:
        return []
    events = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def metrics(stream_events: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    requests = 0
    prompt_tokens = 0
    completion_tokens = 0
    peak = 0
    per_request: list[int] = []
    compactions = 0
    compaction_errors: list[str] = []
    inline_folds = 0
    paper_folds = 0
    summary_prompt = 0
    summary_completion = 0
    shadowed = 0
    first = last = None
    # Per-request usage is only on the CLI event stream (`--json`).
    for event in stream_events:
        if event.get("type") == "status" and event.get("phase") == "step_end":
            usage = event.get("usage") or {}
            if usage:
                prompt = int(usage.get("inputTokens") or 0) + int(usage.get("cacheReadTokens") or 0)
                completion = int(usage.get("outputTokens") or 0)
                if prompt or completion:
                    requests += 1
                    prompt_tokens += prompt
                    completion_tokens += completion
                    per_request.append(prompt)
                    peak = max(peak, prompt)
    for event in events:
        kind = event.get("type", "")
        stamp = event.get("time")
        if isinstance(stamp, int):
            first = stamp if first is None else min(first, stamp)
            last = stamp if last is None else max(last, stamp)
        data = event.get("data") or {}
        if kind == "compaction/summary":
            if data.get("inline") is True or data.get("llmStreamCall") is False:
                inline_folds += 1
            else:
                paper_folds += 1
            usage = data.get("usage") or {}
            summary_prompt += int(usage.get("inputTokens") or 0) + int(usage.get("cacheReadTokens") or 0)
            summary_completion += int(usage.get("outputTokens") or 0)
            shadowed += int(data.get("shadowedTokenCount") or 0)
        elif kind == "compaction/end":
            if data.get("error"):
                compaction_errors.append(str(data["error"]))
            else:
                compactions += 1
    return {
        "requests": requests,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "peak_context": peak,
        "per_request": per_request,
        "compactions": compactions,
        "inline_folds": inline_folds,
        "paper_folds": paper_folds,
        "compaction_errors": compaction_errors,
        "compaction_error_count": len(compaction_errors),
        "summary_prompt_tokens": summary_prompt,
        "summary_completion_tokens": summary_completion,
        "shadowed_tokens": shadowed,
        "wall_s": round((last - first) / 1000, 2) if first and last else None,
    }


def _run_once(task: BenchTask, arm: str, run_dir: Path, timeout_s: int | None = None,
              model: str = "GLM-5.3-Inner") -> dict[str, Any]:
    profile = profile_for(arm)
    run_dir = Path(run_dir)
    clone_workspace(task.workspace, run_dir)
    if (run_dir / ".git").exists():
        pass
    log_path = run_dir.parent / f"{run_dir.name}.jsonl"
    err_path = run_dir.parent / f"{run_dir.name}.stderr.log"
    env = os.environ.copy()
    env["DSH_HOME"] = str(DSH_HOME)
    env["JMS_PROXY_API_KEY"] = api_key()
    env.setdefault("DSH_PERMISSION_MODE", "workspace-write")
    env["SAC_LLM_TIMEOUT"] = env.get("SAC_LLM_TIMEOUT", "180")

    command = [str(DSH), "--profile", profile, "--json", "--approval-policy", "never",
               task.prompt]
    started = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
        try:
            process = subprocess.run(command, cwd=str(run_dir), env=env, stdout=out,
                                     stderr=err, timeout=timeout_s or task.timeout_s)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = -1
    wall = round(time.monotonic() - started, 2)

    final_text = ""
    session_id = None
    events = []
    stream_events = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        stream_events.append(event)
        if event.get("type") == "session":
            session_id = event.get("sessionId")
        if event.get("type") == "final":
            final_text = event.get("text") or ""
    if session_id:
        path = session_log(session_id)
        if path is not None:
            events = read_session(path)
    stats = metrics(stream_events, events)
    stats.update({"wall_s_total": wall, "timed_out": timed_out, "returncode": returncode,
                  "session_id": session_id, "model": model})
    if (run_dir / ".git").exists():
        stats["diff"] = workspace_diff(run_dir)
    else:
        stats["diff"] = ""
    result = {
        "final_text": final_text,
        "parsed": extract_json(final_text),
        "diff": stats.get("diff", ""),
        "workspace": str(run_dir),
        "stderr": err_path.read_text(encoding="utf-8", errors="replace")[-4000:],
    }
    try:
        verdict = task.verify(result)
    except Exception as error:  # noqa: BLE001 - scoring must not kill the matrix
        verdict = {"score": 0.0, "detail": f"verify error: {error}"}
    return {
        "bench": task.bench,
        "task_id": task.task_id,
        "arm": arm,
        "profile": profile,
        "ok": verdict.get("score", 0) >= 0.999 and not timed_out,
        "score": verdict.get("score", 0.0),
        "detail": verdict.get("detail"),
        "metrics": stats,
        "answer": final_text[-2000:],
        "meta": task.meta,
        "run_dir": str(run_dir),
    }

def run_task(task: BenchTask, arm: str, run_dir: Path, timeout_s: int | None = None,
             model: str = "GLM-5.3-Inner", attempts: int = 4) -> dict[str, Any]:
    """Run one task, retrying transient provider failures (5xx / empty reply)."""
    last = None
    for attempt in range(attempts):
        last = _run_once(task, arm, run_dir, timeout_s, model)
        metrics = last.get("metrics") or {}
        wall = metrics.get("wall_s_total") or 0
        log_text = ""
        log_path = Path(run_dir).parent / f"{Path(run_dir).name}.jsonl"
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        transient = ('"code":"SERVER"' in log_text or '"code":"RATE_LIMIT"' in log_text
                     or '"code":"TRANSPORT"' in log_text
                     or 'Connection error' in log_text
                     or metrics.get("requests", 0) == 0)
        retryable = (not last.get("ok")
                     and not metrics.get("timed_out")
                     and wall < 180
                     and transient
                     and attempt < attempts - 1)
        if not retryable:
            break
        last["retried"] = attempt + 1
        time.sleep(10 + 20 * attempt)
    return last
