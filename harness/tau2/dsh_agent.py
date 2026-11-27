"""τ²-bench ↔ DSH adapter.

The official τ²-bench orchestrator (tasks, user simulator, environment, evaluation) runs unchanged in this
process; the agent under test is a DSH session driven through the headless CLI.

One τ² *agent turn* = one DSH *turn*
------------------------------------
`DshAgent.generate_next_message(user_message)` runs ``dsh --profile <p> --json [--session-id <sid>]`` with the
user's message on stdin. Inside that DSH turn the model may call tools several times; every call is served by
the bridge plugin (``plugins/dsh-tau2-bridge``), which forwards it to :class:`BridgeServer` in this process.
The bridge executes it on the live environment through the orchestrator's own path
(``orchestrator._execute_tool_calls`` → ``environment.get_response``), so state changes and error accounting
are exactly those of the built-in agent. Before returning, the bridge writes the official message pair
(``AssistantMessage(tool_calls=[…])`` followed by its ``ToolMessage``) into the orchestrator's trajectory, so
evaluation (action checks, strict replay, NL assertions) sees what it would see for the built-in agent.
The text of the DSH turn's final reply is returned to τ² as the agent's message; only that text reaches the
simulated user, as in the official protocol.

The user simulator's messages become successive *user turns of one DSH session*, which is what puts the SAC
plugin's multi-turn behaviour under test.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from pydantic import BaseModel, Field

from tau2.agent.base.llm_config import LLMConfigMixin
from tau2.agent.base_agent import (
    AgentError,
    HalfDuplexAgent,
    ValidAgentInputMessage,
)
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    ToolCall,
    ToolMessage,
)
from tau2.data_model.tasks import Task
from tau2.environment.tool import Tool
from tau2.registry import registry

AGENT_NAME = "dsh_agent"

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DSH_BIN = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
# The workspace-local DSH home (the one the benchmark harness uses). Inside a DSH session the
# environment carries the real ~/.dsh; never drive benchmark runs against it implicitly.
DEFAULT_DSH_HOME = REPO / "work" / "dshhome"
DEFAULT_RUNS_DIR = REPO / "work" / "tau2" / "runs"

DSH_SETTING_KEYS = (
    "profile",
    "dsh_bin",
    "dsh_home",
    "runs_dir",
    "permission_mode",
    "turn_timeout",
    "max_tool_calls_per_turn",
    "extra_args",
)


class DshSettings(BaseModel):
    """How to launch the DSH process. Values come from ``--agent-llm-args`` (JSON) or the environment."""

    profile: str = "tau2-sac"
    dsh_bin: str = DEFAULT_DSH_BIN
    dsh_home: str = str(DEFAULT_DSH_HOME)
    runs_dir: str = str(DEFAULT_RUNS_DIR)
    permission_mode: str = "workspace-write"
    turn_timeout: float = 1800.0
    max_tool_calls_per_turn: int = 80
    extra_args: list[str] = Field(default_factory=list)

    @classmethod
    def resolve(cls, llm_args: Optional[dict]) -> "DshSettings":
        values: dict[str, Any] = {}
        if llm_args:
            unknown = sorted(set(llm_args) - set(DSH_SETTING_KEYS) - {"temperature", "seed"})
            if unknown:
                logger.warning(f"DshAgent: ignoring unknown agent-llm-args keys {unknown}")
            values = {key: llm_args[key] for key in DSH_SETTING_KEYS if key in llm_args}
        env = os.environ
        defaults = {
            "profile": env.get("TAU2_DSH_PROFILE"),
            "dsh_bin": env.get("TAU2_DSH_BIN"),
            "dsh_home": env.get("TAU2_DSH_HOME"),
            "runs_dir": env.get("TAU2_DSH_RUNS_DIR"),
        }
        for key, value in defaults.items():
            if value and key not in values:
                values[key] = value
        # The DSH process starts in the task directory, so paths must be absolute.
        for key in ("dsh_bin", "dsh_home", "runs_dir"):
            if key in values:
                values[key] = str(Path(values[key]).expanduser().resolve())
        return cls(**values)


class DshState(BaseModel):
    """What the agent remembers between turns: the DSH session id and per-turn bookkeeping."""

    session_id: Optional[str] = None
    turns: int = 0
    dsh_steps: int = 0
    usage: dict = Field(default_factory=dict)


def _add_usage(total: dict, usage: Optional[dict]) -> dict:
    if not usage:
        return total
    for key in ("inputTokens", "outputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens"):
        if isinstance(usage.get(key), (int, float)):
            total[key] = total.get(key, 0) + usage[key]
    return total


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP face of :class:`BridgeServer`; one endpoint, ``POST /call``."""

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        server: BridgeServer = self.server
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if self.headers.get("authorization") != f"Bearer {server.token}":
            self._reply(401, {"error": True, "content": "bad bridge token"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as error:  # noqa: BLE001
            self._reply(400, {"error": True, "content": f"bad bridge request: {error}"})
            return
        self._reply(200, server.agent.execute_tool_call(payload))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ping":
            self._reply(200, {"pong": True, "tool_calls": self.server.agent.tool_calls_total})
        else:
            self._reply(404, {"error": True, "content": "not found"})

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        return


class BridgeServer(ThreadingHTTPServer):
    """Local, token-protected endpoint the DSH plugin calls; serves while the driver waits on DSH."""

    daemon_threads = True

    def __init__(self, agent: "DshAgent"):
        self.agent = agent
        self.token = uuid.uuid4().hex
        super().__init__(("127.0.0.1", 0), BridgeHandler)
        self.thread = threading.Thread(target=self.serve_forever, name="tau2-bridge", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class DshAgent(LLMConfigMixin, HalfDuplexAgent[DshState]):
    """Half-duplex τ² agent whose replies come from a DSH session (one DSH turn per τ² agent turn)."""

    def __init__(
        self,
        tools: list[Tool],
        domain_policy: str,
        llm: Optional[str] = None,
        llm_args: Optional[dict] = None,
        task: Optional[Task] = None,
    ):
        super().__init__(tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args)
        self.task = task
        self.settings = DshSettings.resolve(self.llm_args)
        self.orchestrator = None
        self.task_dir: Optional[Path] = None
        self._server: Optional[BridgeServer] = None
        self._lock = threading.Lock()
        self._turn_tool_calls = 0
        self._abort_reason: Optional[str] = None
        self.tool_calls_total = 0
        self.turns: list[dict] = []

    # ----- wiring with the orchestrator -----------------------------------

    def bind_orchestrator(self, orchestrator) -> None:
        """Give the agent the orchestrator it serves (called right after the orchestrator is built)."""
        self.orchestrator = orchestrator
        self.task = orchestrator.task
        base = Path(self.settings.runs_dir) / f"{orchestrator.domain}__{orchestrator.task.id}__{orchestrator.simulation_id[:8]}"
        self.task_dir = base
        base.mkdir(parents=True, exist_ok=True)
        self._write_bridge_files()

    def _write_bridge_files(self) -> None:
        assert self.task_dir is not None
        schemas = [tool.openai_schema["function"] for tool in self.tools]
        (self.task_dir / "tau2_tools.json").write_text(json.dumps(schemas, indent=1), encoding="utf-8")
        (self.task_dir / "tau2_policy.md").write_text(
            self.domain_policy.strip()
            + '\n\nThe conversation opened with your greeting: "Hi! How can I help you today?"'
            + " It was already sent; do not greet again.",
            encoding="utf-8",
        )

    def _ensure_bridge(self) -> None:
        if self._server is not None:
            return
        if self.orchestrator is None:
            raise AgentError(
                "DshAgent is not bound to an orchestrator. Run through harness/tau2/run.py "
                "(which binds it) or call agent.bind_orchestrator(orchestrator) yourself."
            )
        self._server = BridgeServer(self)
        self._server.start()

    def _abort(self, reason: str) -> None:
        """End the running DSH turn at the next poll (set once; the first reason is kept)."""
        if self._abort_reason is None:
            self._abort_reason = reason
            logger.warning(f"DshAgent: ending the DSH turn: {reason}")

    def execute_tool_call(self, payload: dict) -> dict:
        """Run one DSH-side tool call on the live environment and record it in the official trajectory."""
        tool_call = ToolCall(
            id=str(payload.get("id") or uuid.uuid4().hex),
            name=str(payload.get("name") or ""),
            arguments=payload.get("arguments") or {},
            requestor="assistant",
        )
        with self._lock:
            self._turn_tool_calls += 1
            self.tool_calls_total += 1
            limit = self.settings.max_tool_calls_per_turn
            if self._turn_tool_calls > limit:
                self._abort(f"tool call limit for one turn ({limit}) reached")
                return {"error": True, "content": f"Error: {self._abort_reason}"}
            # The orchestrator's own path: environment.get_response + error accounting.
            self.orchestrator.trajectory.append(AssistantMessage(role="assistant", tool_calls=[tool_call]))
            results = self.orchestrator._execute_tool_calls([tool_call])
            self.orchestrator.trajectory.extend(results)
            # The official loop spends one step on agent→env and one on env→agent.
            self.orchestrator.step_count += 2
            self.orchestrator.environment.sync_tools()
            result = results[0]
            # The official loop checks these limits after every step and ends the
            # simulation; inside a DSH turn the bridge has to end the turn itself,
            # and the orchestrator then records the official termination reason.
            if self.orchestrator.step_count >= self.orchestrator.max_steps:
                self._abort(f"max_steps ({self.orchestrator.max_steps}) reached")
            elif self.orchestrator.num_errors >= self.orchestrator.max_errors:
                self._abort(f"max_errors ({self.orchestrator.max_errors}) reached")
            return {"content": result.content, "error": bool(result.error)}

    # ----- τ² agent protocol ------------------------------------------------

    def get_init_state(self, message_history: Optional[list[Message]] = None) -> DshState:
        return DshState()

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: DshState
    ) -> tuple[AssistantMessage, DshState]:
        if isinstance(message, (ToolMessage, MultiToolMessage)):
            raise AgentError("DshAgent executes its tool calls itself; it never receives tool messages")
        text = message.content or ""
        record = self._run_dsh_turn(text, state)
        self.turns.append(record)
        # One orchestrator step for agent→user, as in the official loop.
        self.orchestrator.step_count += 1
        reply = record["text"] or "I'm sorry, I couldn't produce a reply right now."
        (self.task_dir / "tau2_turns.json").write_text(json.dumps(self.turns, indent=1), encoding="utf-8")
        message = AssistantMessage(
            role="assistant",
            content=reply,
            usage=state.usage or None,
            raw_data={"dsh": record},
        )
        return message, state

    def _run_dsh_turn(self, text: str, state: DshState) -> dict:
        self._ensure_bridge()
        assert self.task_dir is not None and self._server is not None
        settings = self.settings
        state.turns += 1
        self._turn_tool_calls = 0
        command = [
            settings.dsh_bin,
            "--profile",
            settings.profile,
            "--json",
            "--permission-mode",
            settings.permission_mode,
            "--approval-policy",
            "never",
            *([ "--session-id", state.session_id] if state.session_id else []),
            *settings.extra_args,
        ]
        # Start from a clean DSH environment: inside a DSH session the process
        # environment carries that session's DSH_* facts (its home, profile,
        # id), none of which describe the benchmark process.
        env = {key: value for key, value in os.environ.items() if not key.startswith("DSH_")}
        env["DSH_HOME"] = settings.dsh_home
        env["TAU2_BRIDGE_URL"] = self._server.url
        env["TAU2_BRIDGE_TOKEN"] = self._server.token
        env["TAU2_BRIDGE_TOOLS"] = str(self.task_dir / "tau2_tools.json")
        env["TAU2_BRIDGE_POLICY"] = str(self.task_dir / "tau2_policy.md")
        record: dict[str, Any] = {"turn": state.turns, "user_message": text, "command": command, "text": "", "reason": None, "steps": 0, "tool_calls": 0}
        self._abort_reason = None
        out_path = self.task_dir / f"turn{state.turns}.jsonl"
        err_path = self.task_dir / f"turn{state.turns}.stderr.log"
        started = time.perf_counter()
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            try:
                process = subprocess.Popen(  # noqa: S603 (fixed argv, no shell)
                    command,
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=err,
                    cwd=str(self.task_dir),
                    env=env,
                )
            except OSError as error:
                process = None
                record["exit_code"] = -1
                record["error"] = f"could not start dsh: {error}"
            if process is not None:
                try:
                    process.stdin.write(text.encode("utf-8"))
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                # Wait for the turn, ending it when the bridge hit an official
                # limit (max_steps / max_errors), the per-turn tool-call limit,
                # or the wall-clock timeout.
                deadline = time.monotonic() + settings.turn_timeout
                while process.poll() is None:
                    if self._abort_reason is not None:
                        process.kill()
                        record["killed"] = self._abort_reason
                        break
                    if time.monotonic() > deadline:
                        process.kill()
                        record["killed"] = f"turn timed out after {settings.turn_timeout}s"
                        break
                    time.sleep(0.2)
                try:
                    record["exit_code"] = process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    record["exit_code"] = process.wait()
        record["seconds"] = round(time.perf_counter() - started, 2)
        stdout = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
        usage: dict = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "session" and not state.session_id:
                state.session_id = event.get("sessionId")
            elif kind == "final":
                record["text"] = event.get("text") or ""
                record["reason"] = event.get("reason")
            elif kind == "status" and event.get("phase") == "step_end":
                record["steps"] += 1
                _add_usage(usage, event.get("usage"))
            elif kind == "tool_call":
                record["tool_calls"] += 1
            elif kind == "error":
                record["error"] = event.get("message")
        state.dsh_steps += record["steps"]
        _add_usage(state.usage, usage)
        record["usage"] = usage
        record["session_id"] = state.session_id
        record["task_dir"] = str(self.task_dir)
        if record.get("killed") and not record.get("error"):
            record["error"] = record["killed"]
        if record["exit_code"] != 0:
            logger.warning(
                f"DshAgent: DSH turn {state.turns} ended with exit code {record['exit_code']} "
                f"(reason={record['reason']}, error={record.get('error')}); see {self.task_dir}"
            )
        return record

    def stop(self, message: Optional[ValidAgentInputMessage] = None, state: Optional[DshState] = None) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None


# ----- registration ------------------------------------------------------------


def create_dsh_agent(tools, domain_policy, **kwargs) -> DshAgent:
    """Factory registered as ``dsh_agent``; DSH settings arrive in ``llm_args`` (``--agent-llm-args``)."""
    return DshAgent(
        tools=tools,
        domain_policy=domain_policy,
        llm=kwargs.get("llm"),
        llm_args=kwargs.get("llm_args"),
        task=kwargs.get("task"),
    )


def register_agent(name: str = AGENT_NAME) -> None:
    if registry.get_agent_factory(name) is None:
        registry.register_agent_factory(create_dsh_agent, name)


def bind_agent_to_orchestrator() -> None:
    """Make the official batch runner hand every built orchestrator to its agent.

    ``tau2.runner.batch`` imports ``build_orchestrator`` into its own namespace and calls it per
    simulation; wrapping that name is the one seam needed for the agent to reach the orchestrator
    it serves (environment execution, trajectory, error and step counters).
    """
    import tau2.runner.batch as batch

    original = batch.build_orchestrator
    if getattr(original, "_binds_dsh_agent", False):
        return

    def build_and_bind(config, task, **kwargs):
        orchestrator = original(config, task, **kwargs)
        binder = getattr(orchestrator.agent, "bind_orchestrator", None)
        if callable(binder):
            binder(orchestrator)
        return orchestrator

    build_and_bind._binds_dsh_agent = True  # type: ignore[attr-defined]
    batch.build_orchestrator = build_and_bind
