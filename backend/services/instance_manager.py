import asyncio
import json
import logging
import os
import re
import signal
from datetime import datetime
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models.instance import Instance
from backend.models.task import Task

from backend.models.log_entry import LogEntry
from backend.services.stream_parser import StreamParser
from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)


class InstanceManager:
    """Manages multiple Claude Code subprocess instances."""

    def __init__(self, db_factory, broadcaster: WebSocketBroadcaster):
        self.db_factory = db_factory  # async_sessionmaker
        self.broadcaster = broadcaster
        self.parser = StreamParser()
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self._tasks: dict[int, asyncio.Task] = {}  # instance_id -> consumer task
        self._stopping: set[int] = set()  # instance_ids being intentionally stopped
        self._config_dirs: dict[int, str] = {}  # instance_id -> CLAUDE_CONFIG_DIR used
        self._last_stderr: dict[int, str] = {}  # instance_id -> stderr from last run
        self._launch_params: dict[int, dict] = {}  # instance_id -> params for re-launch on rotation

        # PTY persistent-session backend (claude provider only).
        # Runtime-switchable: env USE_PTY_MODE is the boot default, the
        # /api/settings/runtime endpoint can flip it live (affects new
        # launches only; running sessions finish on their current path).
        self._pty_backend = None
        self._pty_enabled = False
        if settings.use_pty_mode:
            self.set_pty_mode(True)

    @property
    def pty_mode_enabled(self) -> bool:
        return self._pty_enabled and self._pty_backend is not None

    async def drain_idle_pty_sessions(self) -> int:
        """Stop idle PTY sessions (called after PTY mode is switched off).
        In-flight turns are untouched and finish on the PTY path."""
        if self._pty_backend is None:
            return 0
        return await self._pty_backend.drain_idle_sessions()

    def set_pty_mode(self, enabled: bool) -> bool:
        """Enable/disable PTY mode at runtime. Returns the effective state.

        The backend is created lazily on first enable and kept on disable
        (it may still manage sessions that started in PTY mode).
        """
        if enabled:
            if self._pty_backend is None:
                try:
                    from claude_pty.adapters.ccm import CCMBackend
                    self._pty_backend = CCMBackend(self)
                    logger.info("PTY mode enabled (claude_pty persistent sessions)")
                except ImportError:
                    logger.warning(
                        "PTY mode requested but claude_pty is not installed; "
                        "staying on `claude -p` mode"
                    )
                    self._pty_enabled = False
                    return False
            self._pty_enabled = True
        else:
            if self._pty_enabled:
                logger.info("PTY mode disabled; new launches use `claude -p`")
            self._pty_enabled = False
        return self._pty_enabled

    async def launch(self, instance_id: int, prompt: str, task_id: int | None = None, cwd: str | None = None, model: str | None = None, resume_session_id: str | None = None, loop_iteration: int | None = None, git_env: dict | None = None, thinking_budget: int | None = None, effort_level: str | None = None, chat_initiated: bool = False, config_dir: str | None = None, provider: str = "claude", enable_workflows: bool = False, enabled_skills: dict | None = None) -> int:
        """Launch a Claude Code subprocess for the given instance.

        If resume_session_id is provided, uses --resume to continue the conversation.
        loop_iteration is recorded on every LogEntry produced by this invocation so
        that loop-task chat history can be grouped by iteration in the frontend.
        """
        provider = (provider or "claude").lower()

        mcp_config_path = None
        if enabled_skills and provider == "claude" and task_id:
            from backend.services.mcp_config import generate_mcp_config
            mcp_config_path = generate_mcp_config(task_id, enabled_skills)

        if provider == "claude" and self.pty_mode_enabled:
            return await self._launch_pty(
                instance_id=instance_id,
                prompt=prompt,
                task_id=task_id,
                cwd=cwd,
                model=model,
                resume_session_id=resume_session_id,
                loop_iteration=loop_iteration,
                git_env=git_env,
                thinking_budget=thinking_budget,
                effort_level=effort_level,
                chat_initiated=chat_initiated,
                config_dir=config_dir,
                enable_workflows=enable_workflows,
                enabled_skills=enabled_skills,
                mcp_config_path=str(mcp_config_path) if mcp_config_path else None,
            )

        cmd = self._build_command(
            provider=provider,
            prompt=prompt,
            model=model,
            resume_session_id=resume_session_id,
            effort_level=effort_level,
            enable_workflows=enable_workflows,
            mcp_config_path=str(mcp_config_path) if mcp_config_path else None,
            enabled_skills=enabled_skills,
        )

        # Must unset CLAUDE_CODE env var to avoid nested session detection
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")}

        # Inject per-project git identity and credentials as environment variables.
        # These take precedence over any global ~/.gitconfig or system credential helper.
        if git_env:
            env.update(git_env)

        # Pool: inject CLAUDE_CONFIG_DIR so this subprocess uses a specific account
        if config_dir and provider == "claude":
            env["CLAUDE_CONFIG_DIR"] = config_dir
            self._config_dirs[instance_id] = config_dir

        # Forward Extended Thinking budget (Claude-specific env var)
        if thinking_budget and thinking_budget > 0 and provider == "claude":
            env["MAX_THINKING_TOKENS"] = str(thinking_budget)

        # Claude Code can output very large NDJSON lines (e.g. Read tool with big files).
        # Default asyncio limit is 64KB which causes LimitOverrunError and kills the consumer.
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.getcwd(),
            env=env,
            limit=10 * 1024 * 1024,  # 10MB line buffer
        )

        self.processes[instance_id] = process

        # Store launch params for potential pool rotation re-launch
        if chat_initiated:
            self._launch_params[instance_id] = {
                "prompt": prompt,
                "task_id": task_id,
                "cwd": cwd,
                "model": model,
                "git_env": git_env,
                "thinking_budget": thinking_budget,
                "effort_level": effort_level,
                "enable_workflows": enable_workflows,
                "enabled_skills": enabled_skills,
            }

        # Update instance record
        async with self.db_factory() as db:
            await db.execute(
                update(Instance)
                .where(Instance.id == instance_id)
                .values(
                    pid=process.pid,
                    status="running",
                    current_task_id=task_id,
                    started_at=datetime.utcnow(),
                    last_heartbeat=datetime.utcnow(),
                )
            )
            # Save cwd to task for session resumption
            if task_id:
                actual_cwd = cwd or os.getcwd()
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(last_cwd=actual_cwd)
                )
            await db.commit()

        # Start consuming stdout
        consumer = asyncio.create_task(
            self._consume_output(instance_id, task_id, process, loop_iteration, chat_initiated, provider)
        )
        self._tasks[instance_id] = consumer

        return process.pid

    async def _launch_pty(
        self,
        instance_id: int,
        prompt: str,
        task_id: int | None,
        cwd: str | None,
        model: str | None,
        resume_session_id: str | None,
        loop_iteration: int | None,
        git_env: dict | None,
        thinking_budget: int | None,
        effort_level: str | None,
        chat_initiated: bool,
        config_dir: str | None,
        enable_workflows: bool,
        enabled_skills: dict | None,
        mcp_config_path: str | None,
    ) -> int:
        """PTY-mode launch: delegate to claude_pty, mirror -p bookkeeping.

        The backend installs a process proxy into self.processes and a
        consumer into self._tasks; events flow back through _process_event,
        so everything downstream (DB, WebSocket, dispatcher wait) is
        unchanged.
        """
        await self._pty_backend.launch_for_ccm(
            instance_id=instance_id,
            prompt=prompt,
            task_id=task_id,
            cwd=cwd,
            model=model if model and model != "default" else None,
            resume_session_id=resume_session_id,
            loop_iteration=loop_iteration,
            git_env=git_env,
            thinking_budget=thinking_budget,
            effort_level=effort_level,
            chat_initiated=chat_initiated,
            config_dir=config_dir,
            enable_workflows=enable_workflows,
            enabled_skills=enabled_skills,
            mcp_config_path=mcp_config_path,
        )

        process = self.processes.get(instance_id)
        pid = getattr(process, "pid", 0) or 0

        async with self.db_factory() as db:
            await db.execute(
                update(Instance)
                .where(Instance.id == instance_id)
                .values(
                    pid=pid,
                    status="running",
                    current_task_id=task_id,
                    started_at=datetime.utcnow(),
                    last_heartbeat=datetime.utcnow(),
                )
            )
            if task_id:
                actual_cwd = cwd or os.getcwd()
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(last_cwd=actual_cwd)
                )
            await db.commit()

        return pid

    def _build_command(
        self,
        provider: str,
        prompt: str,
        model: str | None,
        resume_session_id: str | None,
        effort_level: str | None,
        enable_workflows: bool = False,
        mcp_config_path: str | None = None,
        enabled_skills: dict | None = None,
    ) -> list[str]:
        """Build the subprocess command for a supported coding-agent CLI."""
        if provider == "claude":
            cmd = [
                settings.claude_binary,
                "-p", prompt,
                "--dangerously-skip-permissions",
                "--output-format", "stream-json",
                "--verbose",
            ]
            if resume_session_id:
                cmd.extend(["--resume", resume_session_id])
            if model:
                cmd.extend(["--model", model])
            if effort_level:
                cmd.extend(["--effort", effort_level])
            from backend.services.command_registry import COMMAND_REGISTRY
            disallowed = []
            if not enable_workflows:
                disallowed.append("Workflow")
            if enabled_skills:
                for skill, enabled in enabled_skills.items():
                    if enabled and skill in COMMAND_REGISTRY:
                        disallowed.extend(COMMAND_REGISTRY[skill].disallowed_builtins)
            if disallowed:
                cmd.extend(["--disallowedTools", ",".join(sorted(set(disallowed)))])
            if mcp_config_path and Path(mcp_config_path).exists():
                cmd.extend(["--mcp-config", mcp_config_path])
            return cmd

        if provider == "codex":
            codex_binary = self._resolve_codex_binary()
            if resume_session_id:
                cmd = [codex_binary, "exec", "resume"]
            else:
                cmd = [codex_binary, "exec"]
            cmd.extend([
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
            ])
            if model and model != "default":
                cmd.extend(["--model", model])
            if effort_level and effort_level != "max":
                cmd.extend(["-c", f'model_reasoning_effort="{effort_level}"'])
            if resume_session_id:
                cmd.append(resume_session_id)
            cmd.append(prompt)
            return cmd

        raise ValueError(f"Unsupported CLI provider: {provider}")

    def _resolve_codex_binary(self) -> str:
        """Resolve Codex CLI without relying on the WindowsApps execution alias."""
        configured = settings.codex_binary
        if configured and configured.lower() != "codex":
            return configured

        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            bin_root = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
            candidates = list(bin_root.glob("*/codex.exe"))
            if candidates:
                newest = max(candidates, key=lambda p: p.stat().st_mtime)
                return str(newest)

        return configured or "codex"

    async def _consume_output(self, instance_id: int, task_id: int | None, process: asyncio.subprocess.Process, loop_iteration: int | None = None, chat_initiated: bool = False, provider: str = "claude"):
        """Read NDJSON lines from stdout, parse, store, and broadcast.

        This method MUST keep running until the process closes stdout (EOF).
        Any exception other than CancelledError is caught and logged so that
        a single bad line or transient DB error never kills the whole consumer.
        """
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue

                    if provider == "claude":
                        events = self.parser.parse_line(text)
                    else:
                        parsed = self._parse_codex_line(text)
                        events = [parsed] if parsed else []
                    if not events:
                        continue

                    for event in events:
                        try:
                            await self._process_event(instance_id, task_id, event, loop_iteration)
                        except Exception:
                            logger.exception("Failed to process event for instance %s task %s: %s", instance_id, task_id, event.get("event_type"))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Unexpected error in consume loop for instance %s, continuing", instance_id)

        except asyncio.CancelledError:
            pass
        finally:
            # Wait for process to finish
            await process.wait()
            exit_code = process.returncode

            # Read stderr
            stderr_data = await process.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip() if stderr_data else ""
            if stderr_text:
                lines = stderr_text.splitlines()
                lines = [l for l in lines if not re.sub(r'\x1b\[[0-9;]*m', '', l).strip().startswith("[auto]")]
                stderr_text = "\n".join(lines).strip()
            self._last_stderr[instance_id] = stderr_text

            # If stop() was called, it handles instance + task cleanup — skip here
            if instance_id in self._stopping:
                return

            # Pool rotation for chat-initiated rate limit failures
            if task_id and chat_initiated and exit_code not in (0, -2, 130):
                if await self._try_chat_pool_rotation(instance_id, task_id, exit_code, stderr_text):
                    return

            # Update instance status
            # SIGINT (exit code -2 or 130) = user interrupt, treat as idle not error
            async with self.db_factory() as db:
                interrupted = exit_code in (-2, 130)
                new_status = "idle" if (exit_code == 0 or interrupted) else "error"
                values = {
                    "status": new_status,
                    "pid": None,
                    "current_task_id": None,
                }
                await db.execute(
                    update(Instance).where(Instance.id == instance_id).values(**values)
                )
                # Restore task status for chat-initiated runs (not managed by dispatcher)
                if task_id and chat_initiated:
                    chat_active_statuses = ["executing", "in_progress", "failed", "pending"]
                    if exit_code == 0 or interrupted:
                        result = await db.execute(
                            update(Task)
                            .where(Task.id == task_id, Task.status.in_(chat_active_statuses))
                            .values(status="completed", completed_at=datetime.utcnow(), error_message=None)
                        )
                        if result.rowcount:
                            await self.broadcaster.broadcast("tasks", {
                                "event": "status_change",
                                "task_id": task_id,
                                "new_status": "completed",
                                "instance_id": instance_id,
                            })
                    else:
                        result = await db.execute(
                            update(Task)
                            .where(Task.id == task_id, Task.status.in_(chat_active_statuses))
                            .values(status="failed", error_message=stderr_text[:500] if stderr_text else f"Process exited with code {exit_code}")
                        )
                        if result.rowcount:
                            await self.broadcaster.broadcast("tasks", {
                                "event": "status_change",
                                "task_id": task_id,
                                "new_status": "failed",
                                "instance_id": instance_id,
                            })
                await db.commit()

            # Broadcast completion
            exit_event = {
                "event_type": "process_exit",
                "exit_code": exit_code,
                "stderr": stderr_text[:2000] if stderr_text else None,
            }
            await self.broadcaster.broadcast(f"instance:{instance_id}", exit_event)
            if task_id:
                await self.broadcaster.broadcast(f"task:{task_id}", exit_event)
            await self.broadcaster.broadcast("system", {
                "event": "instance_status",
                "instance_id": instance_id,
                "status": new_status,
                "exit_code": exit_code,
            })

            self.processes.pop(instance_id, None)
            self._tasks.pop(instance_id, None)
            self._launch_params.pop(instance_id, None)

    async def _try_chat_pool_rotation(
        self, instance_id: int, task_id: int, exit_code: int, stderr_text: str,
    ) -> bool:
        """Attempt pool rotation for a chat-initiated process that hit rate limit.

        Returns True if rotation succeeded and a new process was launched.
        """
        try:
            from backend.main import dispatcher
            if not dispatcher or not dispatcher.pool or not dispatcher.pool.enabled:
                return False

            from backend.services.claude_pool import (
                is_pool_rotatable, is_rate_limited, is_auth_failure,
                collect_process_output_for_detection, migrate_session,
            )

            log_contents = await self.get_recent_log_contents(task_id, limit=10)
            combined = collect_process_output_for_detection(stderr_text, log_contents)

            if not is_pool_rotatable(combined):
                return False

            old_config_dir = self._config_dirs.get(instance_id)
            if not old_config_dir:
                return False

            if is_auth_failure(combined):
                dispatcher.pool.mark_auth_failure(old_config_dir)
                logger.warning("Chat pool rotation: account %s auth failure", old_config_dir)
            elif is_rate_limited(combined):
                dispatcher.pool.mark_rate_limited(old_config_dir)
                logger.info("Chat pool rotation: account %s rate-limited", old_config_dir)

            old_account_id = dispatcher.pool.account_id_from_config_dir(old_config_dir)
            excluded = {old_account_id} if old_account_id else set()
            new_config_dir = dispatcher.pool.select(exclude=excluded)

            if not new_config_dir:
                logger.warning("Chat pool rotation: no alternative account for task %d", task_id)
                return False

            async with self.db_factory() as db:
                task = await db.get(Task, task_id)
                if not task or not task.session_id:
                    return False
                session_id = task.session_id
                cwd = task.last_cwd or task.target_repo

            migrate_session(old_config_dir, new_config_dir, session_id)

            new_account_id = dispatcher.pool.account_id_from_config_dir(new_config_dir)
            logger.info("Chat pool rotation: task %d switching %s -> %s",
                        task_id, old_account_id, new_account_id)

            await self.broadcaster.broadcast(f"task:{task_id}", {
                "event_type": "pool_rotation",
                "old_account": old_account_id,
                "new_account": new_account_id,
                "reason": "rate_limit" if is_rate_limited(combined) else "auth_failure",
            })
            await self.broadcaster.broadcast("system", {
                "event": "pool_rotation",
                "task_id": task_id,
                "instance_id": instance_id,
                "old_account": old_account_id,
                "new_account": new_account_id,
            })

            params = self._launch_params.get(instance_id, {})
            await self.launch(
                instance_id=instance_id,
                prompt=params.get("prompt", "continue"),
                task_id=task_id,
                cwd=cwd,
                model=params.get("model"),
                resume_session_id=session_id,
                git_env=params.get("git_env"),
                thinking_budget=params.get("thinking_budget"),
                effort_level=params.get("effort_level"),
                chat_initiated=True,
                config_dir=new_config_dir,
                enable_workflows=params.get("enable_workflows", False),
                enabled_skills=params.get("enabled_skills"),
            )
            return True

        except Exception:
            logger.exception("Chat pool rotation failed for task %d", task_id)
            return False

    def _parse_codex_line(self, line: str) -> dict | None:
        """Normalize Codex CLI JSONL events into the same shape as Claude logs."""
        now = datetime.utcnow().isoformat()
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return {
                "event_type": "message",
                "role": "assistant",
                "content": line,
                "tool_name": None,
                "tool_input": None,
                "tool_output": None,
                "raw_json": None,
                "is_error": False,
                "timestamp": now,
            }

        codex_type = data.get("type") or data.get("event") or data.get("event_type") or "codex_event"
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        item_type = item.get("type")

        event = self._base_codex_event(line, now)

        if codex_type == "item.completed" and item_type == "agent_message":
            event.update({
                "event_type": "message",
                "role": "assistant",
                "content": item.get("text") or "",
            })
        elif codex_type == "item.started" and item_type == "command_execution":
            command = item.get("command") or ""
            event.update({
                "event_type": "tool_use",
                "role": "assistant",
                "content": None,
                "tool_name": "Shell",
                "tool_input": json.dumps({"command": command}, ensure_ascii=False),
            })
        elif codex_type == "item.completed" and item_type == "command_execution":
            command = item.get("command") or ""
            output = item.get("aggregated_output") or ""
            exit_code = item.get("exit_code")
            status = item.get("status") or "completed"
            summary = f"Command {status}"
            if exit_code is not None:
                summary += f" with exit code {exit_code}"
            if output:
                summary += f"\n{output}"
            event.update({
                "event_type": "tool_result",
                "role": "tool",
                "content": None,
                "tool_name": "Shell",
                "tool_input": json.dumps({"command": command}, ensure_ascii=False),
                "tool_output": output or summary,
                "is_error": bool(exit_code not in (None, 0)),
            })
        elif codex_type == "turn.completed":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            event.update({
                "event_type": "system_event",
                "content": "turn.completed",
                "context_usage": self._codex_context_usage(usage) if usage else None,
            })
        elif "error" in codex_type.lower() or data.get("error"):
            message = data.get("message") or data.get("error") or codex_type
            if isinstance(message, (dict, list)):
                message = json.dumps(message, ensure_ascii=False)
            event.update({
                "event_type": "system_event",
                "content": str(message),
                "is_error": True,
            })
        else:
            content = data.get("content") or data.get("message") or data.get("text")
            if content is None and item:
                content = item.get("text") or item.get("command") or item.get("status")
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            # Skip events with no extractable content (heartbeats, metadata),
            # but keep events that carry a session_id
            session_id_present = bool(self._extract_codex_session_id(data))
            if not content and not session_id_present and codex_type not in ("item.started", "item.completed"):
                return None
            tool_input = data.get("tool_input") or data.get("input")
            tool_output = data.get("tool_output") or data.get("output")
            event.update({
                "event_type": "system_event",
                "role": data.get("role") or ("assistant" if "message" in codex_type else None),
                "content": content or codex_type,
                "tool_name": data.get("tool_name") or data.get("name"),
                "tool_input": json.dumps(tool_input, ensure_ascii=False) if isinstance(tool_input, (dict, list)) else tool_input,
                "tool_output": json.dumps(tool_output, ensure_ascii=False) if isinstance(tool_output, (dict, list)) else tool_output,
                "is_error": bool(data.get("is_error") or data.get("error") or "error" in codex_type.lower()),
            })

        session_id = self._extract_codex_session_id(data)
        if session_id:
            event["session_id"] = session_id
        return event

    def _base_codex_event(self, line: str, timestamp: str) -> dict:
        return {
            "event_type": "system_event",
            "role": None,
            "content": None,
            "tool_name": None,
            "tool_input": None,
            "tool_output": None,
            "raw_json": line,
            "is_error": False,
            "timestamp": timestamp,
        }

    def _extract_codex_session_id(self, data: dict) -> str | None:
        session_id = (
            data.get("session_id")
            or data.get("sessionId")
            or data.get("conversation_id")
            or data.get("thread_id")
        )
        if not session_id and isinstance(data.get("session"), dict):
            session_id = data["session"].get("id")
        if not session_id and isinstance(data.get("thread"), dict):
            session_id = data["thread"].get("id")
        return session_id

    def _codex_context_usage(self, usage: dict) -> dict:
        input_tokens = int(usage.get("input_tokens") or 0)
        cached_tokens = int(usage.get("cached_input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        return {
            "input_tokens": max(input_tokens - cached_tokens, 0),
            "cache_read_input_tokens": cached_tokens,
            "cache_creation_input_tokens": 0,
            "output_tokens": output_tokens,
            "total_input_tokens": input_tokens,
        }

    async def _process_event(self, instance_id: int, task_id: int | None, event: dict, loop_iteration: int | None = None):
        """Process a single parsed event: save to DB and broadcast."""
        # Extract session_id, cost, and context usage from event
        session_id = event.pop("session_id", None)
        cost_usd = event.pop("cost_usd", None)
        context_usage = event.pop("context_usage", None)
        if session_id and task_id:
            async with self.db_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(session_id=session_id)
                )
                await db.commit()
        if cost_usd is not None:
            async with self.db_factory() as db:
                await db.execute(
                    update(Instance)
                    .where(Instance.id == instance_id)
                    .values(total_cost_usd=cost_usd)
                )
                await db.commit()

        # Store in DB
        async with self.db_factory() as db:
            entry = LogEntry(
                instance_id=instance_id,
                task_id=task_id,
                event_type=event["event_type"],
                role=event.get("role"),
                content=event.get("content"),
                tool_name=event.get("tool_name"),
                tool_input=event.get("tool_input"),
                tool_output=event.get("tool_output"),
                raw_json=event.get("raw_json"),
                is_error=event.get("is_error", False),
                loop_iteration=loop_iteration,
            )
            db.add(entry)
            await db.commit()

            # Update heartbeat
            await db.execute(
                update(Instance)
                .where(Instance.id == instance_id)
                .values(last_heartbeat=datetime.utcnow())
            )
            await db.commit()

        # Mark task as unread when assistant produces a message or result
        if task_id and event.get("role") == "assistant" and event["event_type"] in ("message", "result"):
            async with self.db_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(has_unread=True)
                )
                await db.commit()

        # Broadcast via WebSocket
        broadcast_data = {k: v for k, v in event.items() if k != "raw_json"}
        if loop_iteration is not None:
            broadcast_data["loop_iteration"] = loop_iteration
        await self.broadcaster.broadcast(f"instance:{instance_id}", broadcast_data)
        if task_id:
            await self.broadcaster.broadcast(f"task:{task_id}", broadcast_data)

        # Persist and broadcast context usage
        if context_usage and task_id:
            async with self.db_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(context_window_usage=context_usage)
                )
                await db.commit()
            await self.broadcaster.broadcast(f"task:{task_id}", {
                "event_type": "context_usage",
                **context_usage,
            })

    async def stop(self, instance_id: int) -> bool:
        """Stop a running Claude Code instance via SIGINT (interrupt).

        Sends SIGINT first so Claude can gracefully save session state,
        then falls back to SIGTERM and SIGKILL if needed.
        """
        process = self.processes.get(instance_id)
        if not process or process.returncode is not None:
            return False

        self._stopping.add(instance_id)
        pty_managed = (
            self._pty_backend is not None
            and instance_id in getattr(self._pty_backend, "_sessions", {})
        )
        if pty_managed:
            # Esc-interrupt the turn, then tear the session down; the proxy's
            # wait() is unblocked by the backend's on_exit.
            await self._pty_backend.stop(instance_id)
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        else:
            import signal
            process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

        # Cancel consumer task
        task = self._tasks.get(instance_id)
        if task and not task.done():
            task.cancel()

        async with self.db_factory() as db:
            inst = await db.get(Instance, instance_id)
            task_id = inst.current_task_id if inst else None
            await db.execute(
                update(Instance)
                .where(Instance.id == instance_id)
                .values(status="idle", pid=None, current_task_id=None)
            )
            if task_id:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.status == "executing")
                    .values(status="completed", error_message=None)
                )
            await db.commit()

        if task_id:
            await self.broadcaster.broadcast(f"task:{task_id}", {
                "event_type": "process_exit",
                "exit_code": process.returncode,
                "stderr": None,
            })

        self.processes.pop(instance_id, None)
        self._stopping.discard(instance_id)
        return True

    def is_running(self, instance_id: int) -> bool:
        process = self.processes.get(instance_id)
        return process is not None and process.returncode is None

    def get_last_stderr(self, instance_id: int) -> str:
        return self._last_stderr.pop(instance_id, "")

    def get_config_dir(self, instance_id: int) -> str | None:
        return self._config_dirs.get(instance_id)

    async def get_recent_log_contents(self, task_id: int, limit: int = 10) -> list[str]:
        """Fetch recent log entry contents for a task (for rate-limit detection)."""
        from backend.models.log_entry import LogEntry
        from sqlalchemy import select as sa_select
        async with self.db_factory() as db:
            result = await db.execute(
                sa_select(LogEntry.content)
                .where(LogEntry.task_id == task_id)
                .order_by(LogEntry.id.desc())
                .limit(limit)
            )
            return [row[0] for row in result.all() if row[0]]
