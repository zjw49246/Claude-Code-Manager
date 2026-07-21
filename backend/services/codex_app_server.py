"""Persistent Codex app-server transport.

The regular ``codex exec resume`` integration starts a new CLI process for
every turn.  App-server keeps configuration, MCP clients, and active threads in
one process while exposing the same persisted Codex thread ids.  This module
adapts one app-server turn to the small subprocess surface InstanceManager
already consumes, so task status/retry/DB logic remains shared with exec mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class CodexAppServerError(RuntimeError):
    """Raised when app-server rejects a request or loses its transport."""


class CodexAppServerBusyError(CodexAppServerError):
    """The requested account home still has an active Codex turn."""


class CodexThreadHomeMismatchError(CodexAppServerError):
    """A thread was routed to a different account without an explicit rebind."""


def normalize_codex_home(codex_home: str | os.PathLike[str] | None = None) -> str:
    """Return the canonical, absolute CODEX_HOME used as the process key."""

    configured = codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    return str(Path(configured).expanduser().resolve(strict=False))


class CodexTurnProcess:
    """Process-like view of one app-server turn.

    InstanceManager only needs stdout/stderr readers, ``wait()``, returncode,
    pid, and interrupt/kill methods.  Keeping that contract lets the existing
    output consumer own all final task/instance state transitions.
    """

    def __init__(
        self,
        pid: int,
        interrupt: Callable[[], Awaitable[None]],
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader(limit=10 * 1024 * 1024)
        self.stderr = asyncio.StreamReader(limit=1024 * 1024)
        self._interrupt = interrupt
        self._done = asyncio.get_running_loop().create_future()

    def feed(self, payload: dict[str, Any]) -> None:
        if self.returncode is not None:
            return
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.stdout.feed_data(line.encode("utf-8") + b"\n")

    def finish(self, returncode: int, stderr: str = "") -> None:
        if self.returncode is not None:
            return
        self.returncode = returncode
        if stderr:
            self.stderr.feed_data(stderr.encode("utf-8", errors="replace"))
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        if not self._done.done():
            self._done.set_result(returncode)

    async def wait(self) -> int:
        return await asyncio.shield(self._done)

    def send_signal(self, sig: int) -> None:
        if sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            asyncio.create_task(self._interrupt_safely())

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    async def _interrupt_safely(self) -> None:
        try:
            await self._interrupt()
        except Exception:
            logger.exception("Codex app-server turn interrupt failed")
            self.finish(130, "Codex turn interrupt failed")


@dataclass
class _TurnContext:
    thread_id: str
    process: CodexTurnProcess
    launch_started: float
    task_id: int | None
    turn_id: str | None = None
    usage: dict[str, int] | None = None
    first_input_seen: bool = False
    first_output_seen: bool = False


class CodexAppServer:
    """One lazily started app-server permanently bound to one CODEX_HOME."""

    def __init__(
        self,
        binary: str,
        request_timeout: float = 30.0,
        *,
        codex_home: str | os.PathLike[str] | None = None,
    ) -> None:
        self.binary = binary
        self.request_timeout = request_timeout
        self.codex_home = normalize_codex_home(codex_home)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._contexts_by_thread: dict[str, _TurnContext] = {}
        self._contexts_by_turn: dict[str, _TurnContext] = {}
        self._stderr_lines: deque[str] = deque(maxlen=100)
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        # App-server keeps completed threads loaded in memory.  The registry
        # uses this set to restart an idle target server before a migrated
        # rollout is resumed there again, avoiding stale in-memory history.
        self._known_threads: set[str] = set()

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> int:
        return self._process.pid if self._process else 0

    @property
    def has_active_turns(self) -> bool:
        return any(
            context.process.returncode is None
            for context in self._contexts_by_thread.values()
        )

    def has_active_thread(self, thread_id: str) -> bool:
        context = self._contexts_by_thread.get(thread_id)
        return bool(context and context.process.returncode is None)

    def knows_thread(self, thread_id: str) -> bool:
        return thread_id in self._known_threads

    async def ensure_started(self) -> None:
        if self.is_alive:
            return
        async with self._start_lock:
            if self.is_alive:
                return
            # Do not let a replacement process start while the previous
            # reader is still failing its pending requests/turns.  Otherwise
            # the stale reader could clear contexts belonging to the new PID.
            if self._reader_task and not self._reader_task.done():
                await self._reader_task
            await self._start()

    async def _start(self) -> None:
        self._stderr_lines.clear()
        codex_home = Path(self.codex_home)
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(codex_home, 0o700)
        except OSError:
            logger.warning("Could not enforce 0700 on CODEX_HOME %s", codex_home)
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in ("CLAUDECODE", "CLAUDE_CODE")
        }
        env["CODEX_HOME"] = self.codex_home
        started = time.perf_counter()
        self._process = await asyncio.create_subprocess_exec(
            self.binary,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=10 * 1024 * 1024,
        )
        process = self._process
        self._reader_task = asyncio.create_task(self._read_loop(process))
        self._stderr_task = asyncio.create_task(self._stderr_loop(process))
        try:
            await self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "claude_code_manager",
                        "title": "Claude Code Manager",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._notify("initialized", {})
        except Exception:
            await self.shutdown()
            raise
        logger.info(
            "Codex app-server ready pid=%s home=%s startup_ms=%.1f",
            self.pid,
            self.codex_home,
            (time.perf_counter() - started) * 1000,
        )

    async def start_turn(
        self,
        *,
        prompt: str,
        cwd: str,
        model: str | None,
        effort: str | None,
        resume_session_id: str | None,
        git_env: dict[str, str] | None,
        task_id: int | None,
    ) -> tuple[CodexTurnProcess, str]:
        await self.ensure_started()
        launch_started = time.perf_counter()
        thread_config: dict[str, Any] = {}
        if git_env:
            # Per-project git credentials must remain thread-scoped.  A global
            # app-server environment would leak one project's identity into
            # every other concurrently running task.
            thread_config["shell_environment_policy"] = {
                "inherit": "all",
                "set": git_env,
            }

        common: dict[str, Any] = {
            "cwd": os.path.abspath(cwd),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if model and model != "default":
            common["model"] = model
        if thread_config:
            common["config"] = thread_config

        if resume_session_id:
            response = await self._request(
                "thread/resume",
                {"threadId": resume_session_id, **common},
            )
        else:
            response = await self._request("thread/start", common)

        thread = response.get("thread") if isinstance(response, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not thread_id:
            raise CodexAppServerError("thread start/resume returned no thread id")
        self._known_threads.add(thread_id)
        existing = self._contexts_by_thread.get(thread_id)
        if existing and existing.process.returncode is None:
            raise CodexAppServerBusyError(
                f"thread {thread_id} already has an active turn"
            )

        async def _interrupt() -> None:
            context = self._contexts_by_thread.get(thread_id)
            if context and context.turn_id:
                await self._request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": context.turn_id},
                )

        turn_process = CodexTurnProcess(self.pid, _interrupt)
        context = _TurnContext(
            thread_id=thread_id,
            process=turn_process,
            launch_started=launch_started,
            task_id=task_id,
        )
        self._contexts_by_thread[thread_id] = context
        # Persist the native thread id through the same event path as exec.
        turn_process.feed({"type": "thread.started", "thread_id": thread_id})

        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": os.path.abspath(cwd),
            "approvalPolicy": "never",
            "model": model if model and model != "default" else None,
            "effort": effort,
        }
        try:
            turn_response = await self._request("turn/start", turn_params)
        except Exception:
            self._contexts_by_thread.pop(thread_id, None)
            turn_process.finish(1, "Codex app-server rejected turn/start")
            raise

        turn = turn_response.get("turn") if isinstance(turn_response, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not turn_id:
            self._contexts_by_thread.pop(thread_id, None)
            turn_process.finish(1, "Codex app-server turn/start returned no turn id")
            raise CodexAppServerError("turn/start returned no turn id")
        context.turn_id = turn_id
        self._contexts_by_turn[turn_id] = context
        logger.info(
            "Codex latency task=%s thread=%s stage=turn_started elapsed_ms=%.1f",
            task_id,
            thread_id,
            (time.perf_counter() - launch_started) * 1000,
        )
        return turn_process, thread_id

    async def steer_turn(self, thread_id: str, content: str) -> bool:
        """Append user input to the currently active regular turn.

        ``expectedTurnId`` makes the request race-safe: if the turn finishes
        between the local context lookup and the RPC, app-server rejects the
        stale steer instead of attaching it to a later turn.
        """
        if not self.is_alive or not thread_id or not content:
            return False
        context = self._contexts_by_thread.get(thread_id)
        if (
            context is None
            or context.turn_id is None
            or context.process.returncode is not None
        ):
            return False

        expected_turn_id = context.turn_id
        try:
            response = await self._request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": expected_turn_id,
                    "input": [{"type": "text", "text": content}],
                },
            )
        except Exception as exc:
            # A normal turn-boundary race and non-steerable turns (review or
            # manual compact) are protocol rejections, not transport crashes.
            logger.info(
                "Codex steer rejected thread=%s turn=%s reason=%s",
                thread_id,
                expected_turn_id,
                exc,
            )
            return False
        return response.get("turnId") == expected_turn_id

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.is_alive or not self._process or not self._process.stdin:
            raise CodexAppServerError("app-server is not running")
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"id": request_id, "method": method, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
        except Exception:
            self._pending.pop(request_id, None)
            raise
        if "error" in response:
            error = response.get("error") or {}
            raise CodexAppServerError(
                f"{method} failed: {error.get('message') or error}"
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise CodexAppServerError("app-server stdin is unavailable")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self._process.stdin.write(payload.encode("utf-8") + b"\n")
            await self._process.stdin.drain()

    async def _read_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    logger.warning("Ignoring malformed Codex app-server output")
                    continue
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    future = self._pending.pop(request_id, None)
                    if future and not future.done():
                        future.set_result(message)
                    continue
                if request_id is not None and message.get("method"):
                    await self._handle_server_request(message)
                    continue
                if message.get("method"):
                    self._handle_notification(message["method"], message.get("params") or {})
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Codex app-server reader failed")
        finally:
            await process.wait()
            detail = "\n".join(self._stderr_lines)[-4000:]
            error = CodexAppServerError(
                f"Codex app-server exited unexpectedly: {detail or 'no stderr'}"
            )
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            for context in list(self._contexts_by_thread.values()):
                context.process.finish(1, str(error))
            self._contexts_by_thread.clear()
            self._contexts_by_turn.clear()

    async def _stderr_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                self._stderr_lines.append(
                    line.decode("utf-8", errors="replace").rstrip()
                )
        except asyncio.CancelledError:
            return

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            # Mirrors --dangerously-bypass-approvals-and-sandbox.  These should
            # not normally arrive because approvalPolicy is "never", but an
            # explicit response prevents a protocol deadlock if they do.
            await self._write({"id": request_id, "result": {"decision": "accept"}})
            return
        if method == "item/permissions/requestApproval":
            # This newer API has a different response schema: grant the exact
            # requested profile for this turn, matching danger-full-access.
            params = message.get("params") or {}
            await self._write({
                "id": request_id,
                "result": {
                    "permissions": params.get("permissions") or {},
                    "scope": "turn",
                },
            })
            return
        if method in {"applyPatchApproval", "execCommandApproval"}:
            # Legacy v1 approval requests use ReviewDecision values.
            await self._write({
                "id": request_id,
                "result": {"decision": "approved"},
            })
            return
        if method == "currentTime/read":
            await self._write({
                "id": request_id,
                "result": {"currentTimeAt": int(time.time())},
            })
            return
        await self._write(
            {
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported request: {method}"},
            }
        )

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        context = self._contexts_by_turn.get(turn_id) if turn_id else None
        if context is None and thread_id:
            context = self._contexts_by_thread.get(thread_id)
        if context is None:
            return

        if method == "turn/started":
            turn = params.get("turn") or {}
            actual_turn_id = turn.get("id")
            if actual_turn_id:
                context.turn_id = actual_turn_id
                self._contexts_by_turn[actual_turn_id] = context
            context.process.feed({"type": "turn.started"})
            return

        if method == "item/started":
            item = params.get("item") or {}
            if item.get("type") == "userMessage" and not context.first_input_seen:
                context.first_input_seen = True
                logger.info(
                    "Codex latency task=%s thread=%s stage=model_input elapsed_ms=%.1f",
                    context.task_id,
                    context.thread_id,
                    (time.perf_counter() - context.launch_started) * 1000,
                )
            normalized = self._normalize_item(item)
            if normalized and normalized.get("type") in {
                "command_execution", "file_change", "mcp_tool_call", "web_search"
            }:
                context.process.feed({"type": "item.started", "item": normalized})
            return

        if method == "item/completed":
            item = params.get("item") or {}
            normalized = self._normalize_item(item)
            if normalized and normalized.get("type") != "user_message":
                context.process.feed({"type": "item.completed", "item": normalized})
            return

        if method == "item/agentMessage/delta":
            if not context.first_output_seen:
                context.first_output_seen = True
                logger.info(
                    "Codex latency task=%s thread=%s stage=first_delta elapsed_ms=%.1f",
                    context.task_id,
                    context.thread_id,
                    (time.perf_counter() - context.launch_started) * 1000,
                )
            context.process.feed(
                {
                    "type": "item.agent_message.delta",
                    "delta": params.get("delta") or "",
                    "item_id": params.get("itemId"),
                }
            )
            return

        if method in {
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
        }:
            context.process.feed(
                {
                    "type": "item.reasoning.delta",
                    "delta": params.get("delta") or "",
                    "item_id": params.get("itemId"),
                }
            )
            return

        if method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage") or {}
            last = token_usage.get("last") or token_usage.get("total") or {}
            context.usage = {
                "input_tokens": int(last.get("inputTokens") or 0),
                "cached_input_tokens": int(last.get("cachedInputTokens") or 0),
                "output_tokens": int(last.get("outputTokens") or 0),
            }
            return

        if method == "error":
            error = params.get("error") or params
            message = error.get("message") if isinstance(error, dict) else str(error)
            context.process.feed({"type": "turn.failed", "error": {"message": message}})
            return

        if method == "turn/completed":
            turn = params.get("turn") or {}
            status = turn.get("status") or "completed"
            error = turn.get("error")
            if status == "completed":
                context.process.feed(
                    {"type": "turn.completed", "usage": context.usage or {}}
                )
                exit_code = 0
                stderr = ""
            else:
                message = (
                    error.get("message") if isinstance(error, dict) else error
                ) or f"Codex turn ended with status {status}"
                context.process.feed(
                    {"type": "turn.failed", "error": {"message": str(message)}}
                )
                exit_code = 1
                stderr = str(message)
            logger.info(
                "Codex latency task=%s thread=%s stage=completed elapsed_ms=%.1f status=%s",
                context.task_id,
                context.thread_id,
                (time.perf_counter() - context.launch_started) * 1000,
                status,
            )
            context.process.finish(exit_code, stderr)
            self._contexts_by_thread.pop(context.thread_id, None)
            if context.turn_id:
                self._contexts_by_turn.pop(context.turn_id, None)

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
        item_type = item.get("type")
        type_map = {
            "userMessage": "user_message",
            "agentMessage": "agent_message",
            "commandExecution": "command_execution",
            "fileChange": "file_change",
            "mcpToolCall": "mcp_tool_call",
            "webSearch": "web_search",
            "todoList": "todo_list",
        }
        normalized = dict(item)
        normalized["type"] = type_map.get(item_type, item_type)
        rename = {
            "aggregatedOutput": "aggregated_output",
            "exitCode": "exit_code",
        }
        for source, target in rename.items():
            if source in normalized:
                normalized[target] = normalized.pop(source)
        if normalized.get("type") == "reasoning":
            pieces = normalized.get("summary") or normalized.get("content") or []
            normalized["text"] = "\n".join(str(piece) for piece in pieces if piece)
        return normalized

    async def shutdown(self) -> None:
        process = self._process
        if not process:
            return
        if process.stdin:
            process.stdin.close()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task),
            return_exceptions=True,
        )
        if self._process is process:
            self._process = None
        self._reader_task = None
        self._stderr_task = None


class CodexAppServerRegistry:
    """Route Codex turns to one persistent app-server per account home.

    ``CODEX_HOME`` is process-scoped, so changing an environment variable per
    thread on one app-server can never provide account isolation.  This facade
    keeps the old InstanceManager-facing surface while enforcing a stable
    thread -> home owner and independent process lifecycle for every account.
    """

    def __init__(self, binary: str, request_timeout: float = 30.0) -> None:
        self.binary = binary
        self.request_timeout = request_timeout
        self._servers: dict[str, CodexAppServer] = {}
        self._thread_owners: dict[str, str] = {}
        self._draining: set[str] = set()
        # A thread rebind spans an optional target-server shutdown. Keep the
        # route reserved across that await so a manual resume or account
        # maintenance operation cannot reopen the source/target midway.
        self._rebindings: dict[str, tuple[str, str]] = {}
        # Number of start/resume RPC sequences admitted for each home but not
        # yet returned to the caller.  Maintenance checks this under the same
        # lock so relogin cannot race between server lookup and context setup.
        self._starting: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def start_turn(
        self,
        *,
        codex_home: str | os.PathLike[str] | None = None,
        **kwargs: Any,
    ) -> tuple[CodexTurnProcess, str]:
        home = normalize_codex_home(codex_home)
        resume_session_id = kwargs.get("resume_session_id")
        reserved_owner = False

        async with self._lock:
            if home in self._draining:
                raise CodexAppServerBusyError(
                    f"Codex account app-server is draining: {home}"
                )
            if resume_session_id:
                if resume_session_id in self._rebindings:
                    raise CodexAppServerBusyError(
                        f"Codex thread {resume_session_id} is being rebound"
                    )
                owner = self._thread_owners.get(resume_session_id)
                if owner is not None and owner != home:
                    raise CodexThreadHomeMismatchError(
                        f"Codex thread {resume_session_id} is bound to {owner}, not {home}; "
                        "migrate and rebind it before resume"
                    )
                if owner is None:
                    # Reserve the route while the RPC is in flight so two
                    # concurrent resumes cannot load one rollout in two homes.
                    self._thread_owners[resume_session_id] = home
                    reserved_owner = True
            server = self._servers.get(home)
            if server is None:
                server = CodexAppServer(
                    self.binary,
                    request_timeout=self.request_timeout,
                    codex_home=home,
                )
                self._servers[home] = server
            self._starting[home] = self._starting.get(home, 0) + 1

        try:
            process, thread_id = await server.start_turn(**kwargs)
        except BaseException:
            async with self._lock:
                starting = self._starting.get(home, 0) - 1
                if starting > 0:
                    self._starting[home] = starting
                else:
                    self._starting.pop(home, None)
                if reserved_owner:
                    if self._thread_owners.get(resume_session_id) == home:
                        self._thread_owners.pop(resume_session_id, None)
            raise

        async with self._lock:
            starting = self._starting.get(home, 0) - 1
            if starting > 0:
                self._starting[home] = starting
            else:
                self._starting.pop(home, None)
            owner = self._thread_owners.get(thread_id)
            if owner is not None and owner != home:
                # This should only be reachable for a broken/malicious server
                # returning another active thread id.  Interrupt this turn
                # instead of letting account ownership silently change.
                process.terminate()
                raise CodexThreadHomeMismatchError(
                    f"Codex thread {thread_id} is already owned by {owner}, not {home}"
                )
            self._thread_owners[thread_id] = home
        return process, thread_id

    async def steer_turn(self, thread_id: str, content: str) -> bool:
        async with self._lock:
            home = self._thread_owners.get(thread_id)
            server = self._servers.get(home) if home else None
        if server is None:
            return False
        return await server.steer_turn(thread_id, content)

    async def rebind_thread(
        self,
        thread_id: str,
        *,
        source_codex_home: str | os.PathLike[str] | None,
        target_codex_home: str | os.PathLike[str],
    ) -> None:
        """Move registry ownership after the rollout was safely copied.

        App-server keeps completed threads in memory.  If the target server has
        loaded this thread before (the B -> A leg of a round trip), an idle
        target process is restarted so its next ``thread/resume`` reads the
        newly copied rollout.  Active target turns are never killed; callers
        receive a retryable busy error instead.
        """

        source = normalize_codex_home(source_codex_home)
        target = normalize_codex_home(target_codex_home)
        if source == target:
            async with self._lock:
                self._thread_owners[thread_id] = target
            return

        restart_server: CodexAppServer | None = None
        async with self._lock:
            if thread_id in self._rebindings:
                raise CodexAppServerBusyError(
                    f"Codex thread {thread_id} is already being rebound"
                )
            owner = self._thread_owners.get(thread_id)
            if owner is not None and owner != source:
                raise CodexThreadHomeMismatchError(
                    f"Codex thread {thread_id} is owned by {owner}, expected {source}"
                )
            source_server = self._servers.get(source)
            if self._starting.get(source, 0) > 0:
                raise CodexAppServerBusyError(
                    f"Codex source account has a start/resume request in flight: {source}"
                )
            if source_server and source_server.has_active_thread(thread_id):
                raise CodexAppServerBusyError(
                    f"Codex thread {thread_id} still has an active turn in {source}"
                )
            if target in self._draining:
                raise CodexAppServerBusyError(
                    f"Codex target account app-server is draining: {target}"
                )
            target_server = self._servers.get(target)
            if target_server and target_server.knows_thread(thread_id):
                if self._starting.get(target, 0) > 0:
                    raise CodexAppServerBusyError(
                        f"Codex target account has a start/resume request in flight "
                        f"and a stale cached copy of thread {thread_id}: {target}"
                    )
                if target_server.has_active_turns:
                    raise CodexAppServerBusyError(
                        f"Codex target account has active turns and a stale cached "
                        f"copy of thread {thread_id}: {target}"
                    )
                self._draining.add(target)
                restart_server = target_server
            self._rebindings[thread_id] = (source, target)

        try:
            if restart_server is not None:
                try:
                    await restart_server.shutdown()
                finally:
                    async with self._lock:
                        if self._servers.get(target) is restart_server:
                            self._servers.pop(target, None)
                        self._draining.discard(target)

            async with self._lock:
                owner = self._thread_owners.get(thread_id)
                if owner is not None and owner != source:
                    raise CodexThreadHomeMismatchError(
                        f"Codex thread {thread_id} changed owner to {owner} "
                        f"while rebinding from {source}"
                    )
                self._thread_owners[thread_id] = target
        finally:
            async with self._lock:
                self._rebindings.pop(thread_id, None)

    async def clear_thread_owner_for_recovery(
        self,
        thread_id: str,
        *,
        expected_codex_home: str | os.PathLike[str],
    ) -> bool:
        """Drop an idle owner reservation after a failed migration rollback.

        Completed turn contexts are already removed by ``turn/completed``;
        only the registry's thread-to-home reservation can remain split from
        the durable task binding. Clearing that one mapping is safer than
        shutting down an account server that may be serving unrelated turns.
        The next resume then cold-routes from the DB-authoritative account.
        """

        expected = normalize_codex_home(expected_codex_home)
        async with self._lock:
            if thread_id in self._rebindings:
                raise CodexAppServerBusyError(
                    f"Codex thread {thread_id} is being rebound"
                )
            owner = self._thread_owners.get(thread_id)
            if owner is None:
                return True
            if owner != expected:
                raise CodexThreadHomeMismatchError(
                    f"Codex thread {thread_id} is owned by {owner}, expected {expected}"
                )
            server = self._servers.get(owner)
            if server and server.has_active_thread(thread_id):
                raise CodexAppServerBusyError(
                    f"Codex thread {thread_id} still has an active turn in {owner}"
                )
            self._thread_owners.pop(thread_id, None)
            return True

    async def begin_home_maintenance(
        self,
        codex_home: str | os.PathLike[str],
        *,
        require_idle: bool = True,
    ) -> bool:
        """Reserve and stop one home until ``end_home_maintenance``.

        This is the relogin/delete primitive: once it returns, new turns for
        the home are rejected even though its old process has already stopped.
        The caller must release the reservation in a ``finally`` block.
        """

        home = normalize_codex_home(codex_home)
        async with self._lock:
            if home in self._draining:
                raise CodexAppServerBusyError(
                    f"Codex account app-server is already draining: {home}"
                )
            if any(home in pair for pair in self._rebindings.values()):
                raise CodexAppServerBusyError(
                    f"Codex account has a thread rebind in flight: {home}"
                )
            server = self._servers.get(home)
            if require_idle and (
                self._starting.get(home, 0) > 0
                or (server is not None and server.has_active_turns)
            ):
                raise CodexAppServerBusyError(
                    f"Codex account still has an active or starting turn: {home}"
                )
            self._draining.add(home)

        if server is not None:
            try:
                await server.shutdown()
            except BaseException:
                async with self._lock:
                    self._draining.discard(home)
                raise

        async with self._lock:
            if server is not None and self._servers.get(home) is server:
                self._servers.pop(home, None)
            for thread_id, owner in list(self._thread_owners.items()):
                if owner == home:
                    self._thread_owners.pop(thread_id, None)
        return server is not None

    async def end_home_maintenance(
        self, codex_home: str | os.PathLike[str],
    ) -> None:
        """Release a reservation created by ``begin_home_maintenance``."""

        home = normalize_codex_home(codex_home)
        async with self._lock:
            self._draining.discard(home)

    async def shutdown_home(
        self,
        codex_home: str | os.PathLike[str],
        *,
        require_idle: bool = True,
    ) -> bool:
        """One-shot idle shutdown; unlike maintenance it immediately reopens."""

        maintenance_started = False
        try:
            stopped = await self.begin_home_maintenance(
                codex_home, require_idle=require_idle,
            )
            maintenance_started = True
            return stopped
        finally:
            if maintenance_started:
                await self.end_home_maintenance(codex_home)

    async def shutdown(self) -> None:
        async with self._lock:
            servers = list(self._servers.items())
            self._draining.update(self._servers)
        results = await asyncio.gather(
            *(server.shutdown() for _, server in servers),
            return_exceptions=True,
        )
        for (home, _), result in zip(servers, results):
            if isinstance(result, Exception):
                logger.error("Failed to stop Codex app-server home=%s: %s", home, result)
        async with self._lock:
            self._servers.clear()
            self._thread_owners.clear()
            self._rebindings.clear()
            self._starting.clear()
            self._draining.clear()
