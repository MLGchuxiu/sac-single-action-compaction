"""OpenAI-compatible chat client used by the SAC test harness.

Only the Python standard library is required. The client exposes a single
``complete()`` call that normalizes the provider's response (content, reasoning,
tool calls, usage, latency) and retries transient failures.
"""
from __future__ import annotations

import json
import os
import random
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE_URL = os.environ.get("SAC_BASE_URL", "https://api.openai.com/v1")


def _read_key() -> str:
    """Resolve the provider key from the environment or the DSH credential store."""
    for name in ("SAC_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip().strip("'\"")
    path = os.path.expanduser("~/.dsh/.credentials.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            match = re.search(r"SAC_API_KEY:\s*(\S+)", handle.read())
        if match:
            return match.group(1).strip().strip("'\"")
    except OSError:
        pass
    raise SystemExit("no API key: set SAC_API_KEY")


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    image_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cached_tokens += other.cached_tokens
        self.image_tokens += other.image_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "image_tokens": self.image_tokens,
        }


@dataclass
class Completion:
    content: str
    reasoning: str
    tool_calls: list[dict[str, Any]]
    usage: Usage
    latency_s: float
    finish_reason: str
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.content.strip() and not self.tool_calls


class LLMError(RuntimeError):
    pass


class Client:
    """Retrying chat client for one provider endpoint."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, key: str | None = None,
                 timeout: float | None = None, max_retries: int | None = None):
        timeout = timeout if timeout is not None else float(os.environ.get("SAC_LLM_TIMEOUT", "180"))
        max_retries = max_retries if max_retries is not None else int(os.environ.get("SAC_LLM_RETRIES", "3"))
        self.base_url = base_url.rstrip("/")
        self.key = key or _read_key()
        self.timeout = timeout
        self.max_retries = max_retries
        self.request_count = 0

    def complete(self, *, model: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 max_tokens: int = 8192, temperature: float = 0.2,
                 tool_choice: str | None = None,
                 purpose: str = "action") -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                request = urllib.request.Request(
                    f"{self.base_url}/chat/completions",
                    data=body,
                    headers={"Authorization": self.key, "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.load(response)
                self.request_count += 1
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                raw_usage = data.get("usage") or {}
                details = raw_usage.get("completion_tokens_details") or {}
                prompt_details = raw_usage.get("prompt_tokens_details") or {}
                usage = Usage(
                    prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                    reasoning_tokens=int(details.get("reasoning_tokens") or 0),
                    cached_tokens=int(prompt_details.get("cached_tokens") or 0),
                    image_tokens=int(prompt_details.get("image_tokens") or 0),
                )
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                calls = []
                for index, call in enumerate(message.get("tool_calls") or []):
                    function = call.get("function") or {}
                    arguments = function.get("arguments") or "{}"
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    calls.append({
                        "id": call.get("id") or f"call_{purpose}_{self.request_count}_{index}",
                        "name": function.get("name") or "",
                        "arguments": arguments,
                    })
                return Completion(
                    content=content,
                    reasoning=message.get("reasoning_content") or "",
                    tool_calls=calls,
                    usage=usage,
                    latency_s=time.monotonic() - started,
                    finish_reason=choice.get("finish_reason") or "",
                    attempts=attempt,
                    raw=data,
                )
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:400]
                last_error = LLMError(f"HTTP {error.code}: {detail}")
                if error.code in (400, 401, 403, 404, 422):
                    raise last_error
            except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError) as error:
                last_error = LLMError(f"{type(error).__name__}: {error}")
            except Exception as error:  # noqa: BLE001 - provider quirks must not kill a run
                last_error = LLMError(f"{type(error).__name__}: {error}")
            if attempt < self.max_retries:
                time.sleep(min(30.0, (2 ** (attempt - 1)) * 1.5) + random.random())
        raise last_error or LLMError("request failed")

    def complete_text(self, *, model: str, messages: list[dict[str, Any]],
                      max_tokens: int = 8192, temperature: float = 0.2,
                      purpose: str = "text") -> Completion:
        """Text-only call that retries once with a larger budget when the model
        spends the entire allowance on reasoning and emits no content."""
        result = self.complete(model=model, messages=messages, max_tokens=max_tokens,
                               temperature=temperature, purpose=purpose)
        if result.empty and result.finish_reason == "length" and result.usage.reasoning_tokens > 0:
            retry_budget = min(8192, max_tokens * 2)
            if retry_budget > max_tokens:
                retry = self.complete(model=model, messages=messages, max_tokens=retry_budget,
                                      temperature=temperature, purpose=purpose)
                retry.usage.add(result.usage)
                retry.attempts += result.attempts
                return retry
        return result