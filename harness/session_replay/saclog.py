"""Replay a DSH `session.v4.jsonl` log: surface reconstruction + SAC diagnostics.

Usage (library):
    s = Session(path)
    s.events[seq]            -> raw event dict
    s.surface_at(seq)        -> list of seqs on the surface *before* event `seq` is applied
    s.surface_after(seq)     -> list of seqs on the surface *after* event `seq` is applied

The log records every surface mutation in `surfaceOp`:
    "append"                       -> push this event's seq
    {"op":"replace", startSeq, endSeq} -> replace [startSeq..endSeq] by this event's seq
"""
import json

# Event types that occupy a surface node.
_SURFACE_TYPES = ("user/message", "assistant/message", "tool/result", "system/message", "developer/message")


def text_of(event):
    d = event.get("data") or {}
    if event.get("type") == "user/message":
        c = d.get("content")
    else:
        c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def tool_calls(event):
    d = event.get("data") or {}
    c = (d.get("message") or {}).get("content")
    if not isinstance(c, list):
        return []
    return [b for b in c if isinstance(b, dict) and b.get("type") == "tool-call"]


class Session:
    def __init__(self, path):
        self.path = path
        self.events = {}      # seq -> event
        self.order = []       # seqs in log order
        self.meta = {}
        self.surface = []     # current surface (seqs)
        self.before = {}      # seq -> surface list before applying that event
        self.after = {}       # seq -> surface list after applying that event
        with open(path, encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                t = o.get("type")
                if t == "session":
                    self.meta = o
                    continue
                seq = o["seq"]
                self.events[seq] = o
                self.order.append(seq)
                op = o.get("surfaceOp")
                if op is None:
                    continue
                self.before[seq] = list(self.surface)
                if op == "append":
                    self.surface.append(seq)
                elif isinstance(op, dict) and op.get("op") == "replace":
                    # The replaced nodes are the shadowed surface events. Their
                    # seqs need not be in range order *or* contiguous as values:
                    # a checkpoint keeps the seq it was written with, so it can
                    # sit before lower-numbered nodes and inside a later range.
                    shadowed = [q for q in (o.get("sourceEventSeqs") or [])
                                if self.events[q].get("type") in _SURFACE_TYPES]
                    if shadowed and seq not in shadowed:
                        first = self.surface.index(shadowed[0])
                        last = self.surface.index(shadowed[-1])
                        if self.surface[first:last + 1] != shadowed:
                            raise RuntimeError(f"shadowed seqs {shadowed} are not contiguous at seq {seq}: {self.surface}")
                    else:
                        s, e = op["startSeq"], op["endSeq"]
                        first = self.surface.index(s)
                        last = len(self.surface) - 1 - self.surface[::-1].index(e)
                    self.surface[first:last + 1] = [seq]
                self.after[seq] = list(self.surface)
        self.pos = {q: i for i, q in enumerate(self.order)}

    # --- lookups ---------------------------------------------------------
    def ev(self, seq):
        return self.events[seq]

    def surface_at(self, seq):
        """Surface as it stood when event `seq` happened (before `seq` is applied).

        Uses the snapshot of the latest surface-mutating event at or before `seq`.
        """
        snap = self.before.get(seq)
        if snap is not None:
            return snap
        i = self.pos.get(seq)
        if i is None:
            return list(self.surface)
        for q in reversed(self.order[:i]):
            snap = self.after.get(q)
            if snap is not None:
                return snap
        return []

    def kind(self, seq):
        e = self.events[seq]
        if e.get("type") == "user/message":
            return (e["data"].get("source") or {}).get("kind")
        return None

    def describe(self, seq):
        e = self.events[seq]
        t = e.get("type")
        d = e.get("data") or {}
        if t == "user/message":
            k = (d.get("source") or {}).get("kind")
            return f"user/{k}"
        if t == "assistant/message":
            return f"assistant(tool_calls={len(tool_calls(e))})"
        if t == "tool/result":
            return "tool/result"
        if t == "system/message":
            return "system"
        return t

    def chars(self, seq):
        e = self.events[seq]
        if e.get("type") == "tool/call":
            return 0
        n = len(text_of(e))
        for b in tool_calls(e):
            n += len(json.dumps(b.get("arguments", ""), ensure_ascii=False))
        return n

    def user_authored(self):
        """User-authored messages (source.kind == 'user') in log order."""
        return [q for q in self.order if self.kind(q) == "user"]

    def requests(self):
        return [q for q in self.order if self.events[q].get("type") == "request/header"]

    def turn_of(self, seq):
        """Turn number of the nearest preceding step/start (or 0)."""
        for q in reversed(self.order):
            if q > seq:
                continue
            e = self.events[q]
            if e.get("type") == "step/start":
                return e["data"]["turn"]
            if e.get("type") == "turn/start":
                return e["data"]["turn"]
        return 0
