"""A fake OpenAI-compatible chat endpoint for offline dry runs (no real model, no tokens).

``FakeLlm`` answers like a tool-using customer-service agent driven by a script:

* for each user utterance, the first ``len(calls)`` model requests return one tool call each
  (``calls[i]`` = ``{"name": …, "arguments": {…}}``); afterwards it returns ``final_text``;
* a SAC state-update request (its last message starts with ``Stop work.``) gets ``recap_text``, so the
  plugin's real fold path runs.

The tool-call count is kept *per user utterance on the server*, not counted from the request history:
SAC folds earlier tool calls out of the history, so a history-based counter would never reach the limit
and the fake would loop forever (it did, during development: 30k requests in ten minutes).

One summary line per request is appended to ``log_path`` (lengths and a hash instead of full texts); the
first agent request's system prompt is written to ``<log_path>.first-system.txt``.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Texts DSH delivers in user role that are not something the user said.
NOT_USER_UTTERANCES = (
    "<system-reminder>",
    "This is an automatically generated checkpoint",
    "Time sampled while preparing",
    "Stop work.",
    "Current runtime context",
)

USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}


def last_user_utterance(messages: list[dict]) -> str | None:
    """The latest message in user role that is the user's own words (None when there is none)."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if isinstance(content, str) and content.strip() and not content.startswith(NOT_USER_UTTERANCES):
            return content
    return None


def is_summary_request(messages: list[dict]) -> bool:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            return text.startswith("Stop work.")
    return False


def _chunk(delta: dict, finish=None, model: str = "fake", usage: dict | None = None) -> str:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    return "data: " + json.dumps(payload) + "\n\n"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"object": "list", "data": [{"id": self.server.model, "object": "model"}]}).encode()
        self._send(200, "application/json", body)

    def do_POST(self) -> None:  # noqa: N802
        server: FakeLlm = self.server
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            request = {}
        messages = request.get("messages") or []
        summary = is_summary_request(messages)
        utterance = last_user_utterance(messages)
        with server.lock:
            server.requests += 1
            number = server.requests
            kind, name, arguments, call_id = "main", None, None, None
            if summary:
                kind = "summary"
            elif utterance is not None and server.issued[utterance] < len(server.calls):
                index = server.issued[utterance]
                server.issued[utterance] += 1
                name = server.calls[index]["name"]
                arguments = server.calls[index].get("arguments") or {}
                call_id = f"call_fake_{number}"
            system = [message.get("content") for message in messages if message.get("role") == "system"]
            system_text = "\n".join(part for part in system if part)
            line = {
                "n": number,
                "kind": kind,
                "stream": bool(request.get("stream")),
                "system_sha": hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:12],
                "system_chars": len(system_text),
                "tool_names": sorted(tool.get("function", {}).get("name") for tool in request.get("tools") or []),
                "roles": [message.get("role") for message in messages],
                "tool_results": len([m for m in messages if m.get("role") == "tool"]),
                "utterance": utterance,
                "tool_call": name,
            }
            if server.log_path is not None:
                Path(server.log_path).parent.mkdir(parents=True, exist_ok=True)
                with Path(server.log_path).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                if kind == "main" and not server.first_system_saved:
                    server.first_system_saved = True
                    Path(f"{server.log_path}.first-system.txt").write_text(system_text, encoding="utf-8")
        model = request.get("model") or server.model
        final_text = server.recap_text if summary else server.final_text
        if request.get("stream"):
            frames = [_chunk({"role": "assistant", "content": ""}, model=model)]
            if name is not None:
                frames.append(_chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}]}, model=model))
                frames.append(_chunk({"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(arguments)}}]}, model=model))
                frames.append(_chunk({}, "tool_calls", model, usage=USAGE))
            else:
                frames.append(_chunk({"content": final_text}, model=model))
                frames.append(_chunk({}, "stop", model, usage=USAGE))
            self._send(200, "text/event-stream", ("".join(frames) + "data: [DONE]\n\n").encode("utf-8"))
            return
        message = {"role": "assistant", "content": None if name is not None else final_text}
        finish = "stop"
        if name is not None:
            message["tool_calls"] = [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
            finish = "tool_calls"
        body = json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": USAGE,
            }
        ).encode("utf-8")
        self._send(200, "application/json", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeLlm(ThreadingHTTPServer):
    daemon_threads = True

    recap_text = (
        "## Handoff Recap\n\n1. Objective - answer the customer's request.\n\n"
        "2. Established facts\n- airports include SFO and JFK.\n\n"
        "3. Applied changes\n- none.\n\n4. Verification\n- none.\n\n5. Dead ends\n- none.\n\n"
        "6. Remaining plan\n- reply to the user.\n\n7. Output contract\n(none)\n\n8. Verbatim values\n- SFO, JFK.\n"
    )

    def __init__(self, calls: list[dict], final_text: str, log_path: Path | None, model: str = "fake-model"):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.calls = calls
        self.final_text = final_text
        self.log_path = log_path
        self.model = model
        self.requests = 0
        self.issued: dict[str, int] = defaultdict(int)
        self.first_system_saved = False
        self.lock = threading.Lock()
        self._thread = threading.Thread(target=self.serve_forever, name="fake-llm", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def start(self) -> "FakeLlm":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
