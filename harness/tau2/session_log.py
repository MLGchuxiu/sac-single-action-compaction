"""Read DSH session logs (``session.v4.jsonl`` / ``.jsonl.zstd``) produced by the τ² runs, with SAC metrics."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness" / "session_replay"))

from saclog import Session, text_of  # noqa: E402

CHARS_PER_TOKEN = 3.5  # the SAC plugin's estimator


def find_session_log(dsh_home: Path, session_id: str) -> Path | None:
    """Locate the persisted log of one DSH session (DSH stores it under a directory named after the cwd)."""
    if not session_id:
        return None
    sessions = Path(dsh_home) / "sessions"
    if not sessions.is_dir():
        return None
    for path in sessions.rglob("session.v4.jsonl*"):
        if path.parent.name == session_id:
            return path
    return None


def read_events(path: Path) -> list[dict]:
    """Parse a session log, decompressing ``.zstd`` when needed."""
    if path.suffix == ".zstd":
        raw = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    else:
        raw = path.read_bytes()
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def load_session(path: Path) -> Session:
    """A ``saclog.Session`` for a (possibly compressed) log."""
    if path.suffix != ".zstd":
        return Session(str(path))
    raw = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        handle.write(raw)
        return Session(handle.name)


def user_messages(session: Session) -> list[int]:
    """Seqs of the messages that carry the user's own words (``source.kind`` ``user`` or ``goal``)."""
    return [seq for seq in session.order if session.kind(seq) in ("user", "goal")]


def folds(session: Session) -> list[dict]:
    """One entry per SAC fold: the checkpoint seq, the shadowed seqs, and which user messages it swallowed."""
    out = []
    for seq in session.order:
        if session.kind(seq) != "compact-checkpoint":
            continue
        shadowed = [q for q in (session.events[seq].get("sourceEventSeqs") or []) if q in session.events]
        out.append(
            {
                "checkpoint": seq,
                "shadowed": len([q for q in shadowed if session.events[q]["type"] in ("user/message", "assistant/message", "tool/result")]),
                "user_msgs": [q for q in shadowed if session.kind(q) in ("user", "goal")],
                "chars": session.chars(seq),
            }
        )
    return out


def context_tokens(session: Session) -> dict:
    """Surface size in estimated tokens at every model step (what SAC exists to keep small)."""
    sizes = []
    for seq in session.order:
        if session.events[seq]["type"] != "step/start":
            continue
        surface = session.surface_at(seq)
        sizes.append(sum(session.chars(node) for node in surface) / CHARS_PER_TOKEN)
    if not sizes:
        return {"steps": 0, "mean": 0.0, "peak": 0.0, "final": 0.0}
    final = sum(session.chars(node) for node in session.surface) / CHARS_PER_TOKEN
    return {"steps": len(sizes), "mean": round(sum(sizes) / len(sizes)), "peak": round(max(sizes)), "final": round(final)}


REQUESTS_MARKER = "[SAC EARLIER USER REQUESTS]"

# The Objective line of a recap, whatever heading style the model used (same pattern as
# harness/session_replay/objectives.py).
_OBJECTIVE = re.compile(
    r"(?im)^[ \t]*(?:#+[ \t]*)?(?:\*\*)?[ \t]*(?:\*{0,2}1[ \t]*\.[ \t]*\*{0,2})?(?:\*\*)?[ \t]*(?:objective|目标)"
    r"\**[ \t]*[:：]?[ \t]*\**[ \t]*\n?(.*?)"
    r"(?:\n[ \t]*\n|\n[ \t]*(?:#+|\*\*|__)?[ \t]*2[.\s]|\n###|\Z)"
)


def objective_line(text: str) -> str:
    """The `1. Objective` line of a recap (``'(no objective line)'`` when the recap has none)."""
    match = _OBJECTIVE.search(text or "")
    if match is None:
        return "(no objective line)"
    return re.sub(r"\s+", " ", match.group(1)).strip()


def state_recaps(session: Session) -> list[dict]:
    """One entry per fold: the recap's raw Objective line and the user messages it was written after."""
    users = user_messages(session)
    out = []
    for seq in session.order:
        event = session.events[seq]
        if event["type"] != "compaction/summary":
            continue
        data = event["data"] or {}
        raw = "".join(block.get("text", "") for block in (data.get("rawOutput") or data.get("summary") or []))
        before = [u for u in users if u < seq]
        # The checkpoint is the next user/message event after the summary record; the seqs it
        # replaced are the fold's span.
        checkpoint = next((q for q in session.order if q > seq and session.events[q]["type"] == "user/message"), None)
        span = (session.events[checkpoint].get("sourceEventSeqs") or []) if checkpoint is not None else []
        out.append({
            "seq": seq,
            "turn": session.turn_of(seq),
            "objective": objective_line(raw),
            "recap_chars": len(raw),
            "user_before": [{"seq": u, "text": text_of(session.events[u]).strip()} for u in before],
            "swallowed_user": [u for u in users if u in span],
            # True when the folded span begins after the newest user message: a working-state fold
            # (plugin 1.2.0) or the single legacy segment (1.1.0, EoCC), whose Objective should
            # describe that latest request. A finished-turn or archive fold lies before it by design.
            "after_latest": bool(before) and bool(span) and min(span) > before[-1],
        })
    return out


def user_message_visibility(session: Session) -> dict:
    """How long each user-authored message stays visible verbatim to the model.

    For every user message (``source.kind`` ``user`` / ``goal``) count the model steps after it
    arrived at whose request still showed it: either the message itself is on the step's surface, or
    an archive checkpoint on that surface quotes it verbatim (``[SAC EARLIER USER REQUESTS]``).
    The first message of a session is the task anchor in every arm, so the summary lines concern the
    *later* messages — the ones a fold may swallow.
    """
    users = user_messages(session)
    steps = [seq for seq in session.order if session.events[seq]["type"] == "step/start"]
    rows = []
    for seq in users:
        head = text_of(session.events[seq]).strip()[:200]
        after = [step for step in steps if step > seq]
        shown = 0
        for step in after:
            surface = session.surface_at(step)
            if seq in surface:
                shown += 1
            elif head:
                for node in surface:
                    if session.kind(node) == "compact-checkpoint" and head in text_of(session.events[node]):
                        shown += 1
                        break
        rows.append({"seq": seq, "steps_after": len(after), "steps_shown": shown, "ratio": shown / len(after) if after else None})
    later = [row for row in rows[1:] if row["steps_after"] > 0]
    ratios = [row["ratio"] for row in later]
    return {
        "user_messages": len(rows),
        "later_messages": len(later),
        "later_fully_visible": sum(1 for ratio in ratios if ratio == 1.0),
        "later_partially_folded": sum(1 for ratio in ratios if 0 < ratio < 1.0),
        "later_min_ratio": min(ratios) if ratios else None,
        "later_mean_ratio": sum(ratios) / len(ratios) if ratios else None,
        "rows": rows,
    }


def token_usage(session: Session) -> dict:
    """Token usage of one simulation, read from the DSH session log.

    In an ``assistant/message`` event ``usage.inputTokens`` is the *new* (uncached) input,
    ``cacheReadTokens`` the cached input and ``outputTokens`` what the model generated (reasoning
    included); ``totalTokens`` is the whole request (new + cached input + output). The JSON stream's
    per-step usage carries the same fields, so summing ``inputTokens`` alone under-counts the input
    heavily once the provider's prefix cache is warm. The state-update calls (purpose
    ``compaction``) are only recorded under ``compaction/summary``.
    """
    agent_new = agent_cached = agent_other = agent_out = state_in = state_out = 0
    prompts = []
    for seq in session.order:
        event = session.events[seq]
        data = event.get("data") or {}
        usage = data.get("usage") or {}
        if event["type"] == "assistant/message":
            new = usage.get("inputTokens") or 0
            cached = usage.get("cacheReadTokens") or 0
            other = usage.get("cacheWriteTokens") or 0
            agent_new += new
            agent_cached += cached
            agent_other += other
            agent_out += usage.get("outputTokens") or 0
            prompts.append(new + cached + other)
        elif event["type"] == "compaction/summary":
            state_in += usage.get("inputTokens") or 0
            state_out += usage.get("outputTokens") or 0
    return {
        "steps": len(prompts),
        "agent_requests": len(prompts),
        "agent_new_input": agent_new,
        "agent_cached_input": agent_cached,
        "agent_other_input": agent_other,
        "agent_output": agent_out,
        "state_input": state_in,
        "state_output": state_out,
        "state_calls": sum(1 for seq in session.order if session.events[seq]["type"] == "compaction/summary"),
        "prompt_tokens_total": sum(prompts),
        "prompt_tokens_mean": round(sum(prompts) / len(prompts)) if prompts else 0,
        "prompt_tokens_peak": max(prompts) if prompts else 0,
        "prompt_tokens_first": prompts[0] if prompts else 0,
        "prompts": prompts,
    }


def classify_folds(session: Session) -> dict:
    """Check every fold against the 1.2.0 rules.

    A working-state or finished-turn fold must never contain a user message. An
    archive fold may: it releases the oldest pinned requests, but only lawfully
    if it quotes each of them verbatim in its ``[SAC EARLIER USER REQUESTS]``
    section. Returns the folds with their classification and the violations.
    """
    classified, violations = [], []
    for fold in folds(session):
        checkpoint_text = text_of(session.events[fold["checkpoint"]])
        archive = REQUESTS_MARKER in checkpoint_text
        quoted, unlawful = [], []
        for seq in fold["user_msgs"]:
            head = text_of(session.events[seq]).strip()[:200]
            if archive and head and head in checkpoint_text:
                quoted.append(seq)
            else:
                unlawful.append(seq)
        violations.extend({"checkpoint": fold["checkpoint"], "user_seq": seq, "archive": archive} for seq in unlawful)
        classified.append({**fold, "archive": archive, "quoted_verbatim": quoted, "unlawful": unlawful})
    return {"folds": classified, "violations": violations}


def sac_integrity(session: Session) -> dict:
    """Does the log obey the 1.2.0 invariants?

    * no working-state / finished-turn fold swallows a user message;
    * every user message an archive fold swallows is quoted verbatim in the archive;
    * reports folds, state-update failures, steps and the final surface layout.
    """
    users = user_messages(session)
    result = classify_folds(session)
    all_folds = folds(session)
    archived = [seq for fold in result["folds"] if fold["archive"] for seq in fold["quoted_verbatim"]]
    # A request still on the surface, or quoted in an archive checkpoint, is available to the model.
    available = [seq for seq in users if seq in session.surface or any(seq in fold["quoted_verbatim"] for fold in result["folds"])]
    layout = [(session.kind(seq) or session.events[seq]["type"]) for seq in session.surface]
    return {
        "user_messages": len(users),
        "folded_user_messages": [v["user_seq"] for v in result["violations"]],
        "archived_user_messages": len(archived),
        "user_messages_available_at_end": len(available),
        "folds": len(all_folds),
        "archive_folds": sum(1 for fold in all_folds if REQUESTS_MARKER in text_of(session.events[fold["checkpoint"]])),
        "state_checkpoints_on_surface": layout.count("compact-checkpoint"),
        "compaction_errors": sum(1 for seq in session.order if session.events[seq]["type"] == "compaction/end" and "error" in session.events[seq]["data"]),
        # A fold the backend refused because the recap was not smaller than the span it replaced:
        # the summarizer call was made and paid for, but nothing was written.
        "not_smaller_refusals": sum(
            1
            for seq in session.order
            if session.events[seq]["type"] == "compaction/end" and "not smaller" in str(session.events[seq]["data"].get("error") or "")
        ),
        "steps": sum(1 for seq in session.order if session.events[seq]["type"] == "step/start"),
        "final_layout": layout,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate a DSH session log and write a decompressed copy for the replay tools.")
    parser.add_argument("session_id")
    parser.add_argument("--dsh-home", default=None, help="default: the workspace-local DSH home")
    parser.add_argument("--out", default=None, help="default: work/tau2/sessions/<session-id>.jsonl")
    args = parser.parse_args()
    dsh_home = Path(args.dsh_home or ROOT / "work" / "dshhome")
    log = find_session_log(dsh_home, args.session_id)
    if log is None:
        print(f"no session log for {args.session_id} under {dsh_home / 'sessions'}", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else ROOT / "work" / "tau2" / "sessions" / f"{args.session_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(subprocess.run(["zstd", "-dc", str(log)], capture_output=True, check=True).stdout) if log.suffix == ".zstd" else out.write_bytes(log.read_bytes())
    session = Session(str(out))
    integrity = sac_integrity(session)
    print(f"log:        {log}")
    print(f"decompressed: {out}")
    print(f"steps {integrity['steps']}, folds {integrity['folds']} (archive {integrity['archive_folds']}), user messages {integrity['user_messages']}, "
          f"folded {integrity['folded_user_messages']}, available at end {integrity['user_messages_available_at_end']}, "
          f"state-update failures {integrity['compaction_errors']}")
    print(f"context tokens: {context_tokens(session)}")
    print(f"final surface: {integrity['final_layout']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
