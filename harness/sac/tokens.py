"""Token accounting for the SAC harness.

The provider exposes no tokenizer endpoint, so the harness combines

* a script-aware character heuristic (CJK ≈ 1 token/char, Latin ≈ 1 token/4 chars),
* a per-model calibration factor learned online from ``usage.prompt_tokens``.

The calibration matters because the whole study is about staying inside a hard
input budget: an estimator that drifts low would let requests overflow, one that
drifts high would compact too eagerly and distort the comparison.
"""
from __future__ import annotations

import math
import re

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_WS = re.compile(r"\s+")


def estimate_text(text: str) -> float:
    """Script-aware token estimate for one string."""
    if not text:
        return 0.0
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return cjk * 1.0 + rest / 3.6


def estimate_messages(messages: list[dict]) -> int:
    """Estimate the prompt size of a message list, including tool schemas' call sites."""
    total = 0.0
    for message in messages:
        total += 4  # role/formatting overhead
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_text(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind == "text":
                    total += estimate_text(part.get("text", ""))
                elif kind in ("image_url", "image"):
                    total += 780  # observed ≈200-800 image tokens for small images
                else:
                    total += 8
        for call in message.get("tool_calls") or []:
            total += estimate_text(call.get("function", {}).get("arguments", "")) + 12
    return int(math.ceil(total))


def estimate_tools(tools: list[dict] | None) -> int:
    """Estimate the fixed per-request cost of the tool schema block."""
    if not tools:
        return 0
    import json
    return int(estimate_text(json.dumps(tools, ensure_ascii=False)))


class TokenMeter:
    """Per-model calibration on top of the heuristic estimator."""

    def __init__(self) -> None:
        self._scale: dict[str, float] = {}
        self._samples: dict[str, int] = {}

    def scale(self, model: str) -> float:
        return self._scale.get(model, 1.0)

    def estimate(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> int:
        raw = estimate_messages(messages) + estimate_tools(tools)
        return int(math.ceil(raw * self.scale(model)))

    def observe(self, model: str, estimated: int, actual: int) -> None:
        """Fold one provider-reported prompt size into the model's scale."""
        if estimated <= 0 or actual <= 0:
            return
        ratio = actual / estimated
        # Ignore implausible readings (provider caching quirks, truncated calls).
        if not 0.4 <= ratio <= 3.0:
            return
        count = self._samples.get(model, 0)
        current = self._scale.get(model, 1.0)
        # Exponential moving average, faster on the first few samples.
        alpha = 0.5 if count < 3 else 0.12
        self._scale[model] = (1 - alpha) * current + alpha * ratio
        self._samples[model] = count + 1

    def stats(self) -> dict[str, dict[str, float]]:
        return {
            model: {"scale": round(value, 4), "samples": self._samples.get(model, 0)}
            for model, value in self._scale.items()
        }