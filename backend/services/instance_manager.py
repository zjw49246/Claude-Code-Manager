import asyncio
import json
import logging
import os
import re
import signal
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
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
        # instance_id -> consecutive transient-overload retry count. Survives
        # the in-place relaunch (launch() resets _launch_params, so this can't
        # live there); cleared on success / give-up / stop.
        self._transient_attempts: dict[int, int] = {}
        # instance_ids whose CURRENT turn emitted a transient server-side
        # 429/overload error event. Turn-scoped: reset at launch(), set in
        # _process_event. The reliable signal in PTY mode, where the aborted
        # turn still reports exit_code 0.
        self._transient_seen: set[int] = set()
        # PTY 权限透传：request_id -> {session_id, task_id, tool_name, expires_at}
        # bridge HTTP 线程收到 CC 的权限请求后经 _loop 调度进事件循环
        self._pty_permissions: dict[str, dict] = {}
        self._loop = None  # 主事件循环，lifespan 启动时注入

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

    async def inject_pty_message(self, session_id: str, content: str) -> bool:
        """Inject text into a live PTY session (PTY-only).

        Looked up by Claude session_id — the chat path picks a different
        instance per message, so task.instance_id is NOT a reliable key.
        Delivered as a channel notification; CC consumes it at the next
        tool-call boundary (mid-turn) or at the start of the next turn.
        Returns False when PTY mode is off, no live session exists, or
        injection fails.
        """
        if self._pty_backend is None or not content or not session_id:
            return False
        session = None
        key = None
        for k, sess in self._pty_backend._sessions.items():
            if sess.session_id == session_id and sess.is_alive:
                session, key = sess, k
                break
        if session is None:
            return False
        # 仅允许注入到【正在运行的 turn】：turn 结束后 consumer 退出，
        # 此时注入的 channel 消息会唤醒一个无人采集的"孤儿 turn"——
        # 回复只进 JSONL，CCM 永远看不到（生产 task 51 实录）。
        consumer = self._pty_backend._consumers.get(key)
        if consumer is None or consumer.done():
            logger.info(
                "PTY inject rejected for session %s: no running turn", session_id
            )
            return False
        try:
            return await session.inject(content)
        except Exception:
            logger.exception("PTY inject failed for session %s", session_id)
            return False

    async def release_pty_session(self, session_id: str) -> None:
        """Return a PTY session to nothing — stop it and remove from the pool.
        Used when a workload (e.g. a loop task) is finished with its session.
        No-op when PTY mode is not in use."""
        if self._pty_backend is None or not session_id:
            return
        try:
            await self._pty_backend._pool.remove(session_id)
        except Exception:
            logger.exception("Failed to release PTY session %s", session_id)

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
                    # 权限透传：CC 的权限请求经 BridgeHub 转给前端卡片，
                    # 不注册的话 channel server 120s 超时默认 deny
                    self._pty_backend._bridge.on_permission_request(
                        self._on_pty_permission_request
                    )
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
            # NOTE: idle-session drain on toggle-off is the API layer's job
            # (PUT /api/settings/runtime awaits drain_idle_pty_sessions) —
            # this sync method must stay loop-free.
            self._pty_enabled = False
        return self._pty_enabled

    async def launch(self, instance_id: int, prompt: str, task_id: int | None = None, cwd: str | None = None, model: str | None = None, resume_session_id: str | None = None, loop_iteration: int | None = None, git_env: dict | None = None, thinking_budget: int | None = None, effort_level: str | None = None, chat_initiated: bool = False, config_dir: str | None = None, provider: str = "claude", enable_workflows: bool = False, enabled_skills: dict | None = None, system_prompt_mode: str | None = None) -> int:
        """Launch a Claude Code subprocess for the given instance.

        If resume_session_id is provided, uses --resume to continue the conversation.
        loop_iteration is recorded on every LogEntry produced by this invocation so
        that loop-task chat history can be grouped by iteration in the frontend.
        """
        provider = (provider or "claude").lower()

        # New turn → clear the per-turn transient-overload flag.
        self._transient_seen.discard(instance_id)

        mcp_config_path = None
        if provider == "claude" and task_id:
            from backend.services.mcp_config import generate_mcp_config
            mcp_config_path = generate_mcp_config(task_id, enabled_skills or {})

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
            system_prompt_mode=system_prompt_mode,
            cwd=cwd,
            task_id=task_id,
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

        # Disable CC's auto-compact — CCM manages context/compaction itself
        env["DISABLE_AUTO_COMPACT"] = "true"

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
        system_prompt_mode: str | None = None,
        cwd: str | None = None,
        task_id: int | None = None,
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
            from backend.services.skill_loader import discover_skills, build_skill_prompt_file, get_skill_disallowed_tools
            skills = discover_skills(project_dir=cwd)
            disallowed = []
            if not enable_workflows:
                disallowed.append("Workflow")
            disallowed.extend(get_skill_disallowed_tools(skills, enabled_skills))
            if disallowed:
                cmd.extend(["--disallowedTools", ",".join(sorted(set(disallowed)))])
            if mcp_config_path and Path(mcp_config_path).exists():
                cmd.extend(["--mcp-config", mcp_config_path])
            # Skill prompt injection
            skill_prompt_path = build_skill_prompt_file(skills, enabled_skills, task_id)
            if skill_prompt_path:
                cmd.extend(["--append-system-prompt-file", skill_prompt_path])
            if system_prompt_mode and settings.append_system_prompt_file:
                sp_path = Path(settings.append_system_prompt_file)
                if not sp_path.is_absolute():
                    sp_path = Path(settings.worker_deploy_source_dir) / sp_path
                if sp_path.exists():
                    flag = "--system-prompt-file" if system_prompt_mode == "replace" else "--append-system-prompt-file"
                    cmd.extend([flag, str(sp_path)])
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
        _assistant_texts: list[str] = []
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
                            if event.get("event_type") in ("message", "result") and event.get("role") == "assistant":
                                c = event.get("content") or ""
                                if c:
                                    _assistant_texts.append(c)
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

            # Empty-reply retry: if chat turn produced only "No response requested."
            # or similar non-response, re-enqueue the original prompt once.
            _NO_RESPONSE_PATTERNS = {"no response requested.", "no response requested", "no response needed."}
            if (
                task_id
                and chat_initiated
                and exit_code == 0
                and instance_id in self._launch_params
                and not self._launch_params[instance_id].get("_retried")
            ):
                combined = " ".join(_assistant_texts).strip().lower().rstrip(".")
                if not _assistant_texts or combined in _NO_RESPONSE_PATTERNS:
                    params = self._launch_params[instance_id]
                    params["_retried"] = True
                    logger.warning(
                        "Task %d got empty/non-response (%r), re-enqueueing prompt",
                        task_id, combined[:80],
                    )
                    from backend.main import dispatcher
                    from backend.services.dispatcher import PRIORITY_USER
                    await dispatcher.enqueue_message(
                        task_id=task_id,
                        prompt=params["prompt"],
                        priority=PRIORITY_USER,
                        source="retry",
                    )
                    # Still clean up instance below so it's available for the retry
                    # fall through to normal cleanup

            if task_id and chat_initiated and exit_code not in (0, -2, 130):
                # Transient server-side 429/overload: wait + retry same account
                # (checked before rotation; the two failures are exclusive).
                if await self._try_chat_transient_retry(instance_id, task_id, exit_code, stderr_text):
                    return
                # Pool rotation for chat-initiated rate limit failures
                if await self._try_chat_pool_rotation(instance_id, task_id, exit_code, stderr_text):
                    return
            elif task_id and chat_initiated:
                # Clean turn — drop any transient-retry tally for this instance.
                self._transient_attempts.pop(instance_id, None)

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

            # 原生子 agent（native-monitor 等）生命周期跟 session 走——
            # session 退出/重建时一律标 completed，否则 UI 上永远显示 running。
            # CCM 自己的 monitor 子 agent（source="ccm"）有独立进程，不跟主
            # session 走，必须排除，否则 chat turn 结束就误杀 monitor。
            # 但如果有 native-monitor 在 running，说明 monitor 被进程退出打断，
            # 需要 auto-resume 让主 agent 处理积压的 <task-notification>。
            if task_id:
                from backend.models.sub_agent import SubAgentSession
                has_pending_monitors = False
                async with self.db_factory() as db:
                    stale = await db.execute(
                        select(SubAgentSession).where(
                            SubAgentSession.task_id == task_id,
                            SubAgentSession.status == "running",
                            SubAgentSession.source != "ccm",
                        )
                    )
                    for sa in stale.scalars().all():
                        if sa.agent_type in ("native-monitor", "monitor"):
                            has_pending_monitors = True
                        sa.status = "completed"
                        sa.completed_at = datetime.utcnow()
                    await db.commit()

                # Auto-resume: native monitor 随进程退出，
                # resume 让主 agent 拿到 <task-notification> 并做出反应
                if has_pending_monitors and exit_code == 0 and chat_initiated:
                    try:
                        from backend.main import dispatcher
                        from backend.services.dispatcher import PRIORITY_MONITOR_COMPLETE
                        await dispatcher.enqueue_message(
                            task_id=task_id,
                            prompt=(
                                "[Monitor 通知] 你之前启动的 Monitor 已有结果。"
                                "请检查 monitor 的 task-notification 并根据结果决定下一步操作。"
                            ),
                            priority=PRIORITY_MONITOR_COMPLETE,
                            source="monitor:native-exit-resume",
                            user_message_text="[Monitor] 后台监控已产生通知，自动恢复会话",
                        )
                        logger.info(
                            "Task %d had pending native monitors on exit, enqueued auto-resume",
                            task_id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to enqueue monitor auto-resume for task %s", task_id,
                        )

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

    async def _try_chat_transient_retry(
        self, instance_id: int, task_id: int, exit_code: int, stderr_text: str,
    ) -> bool:
        """Wait out a transient server-side 429/overload for a chat turn and
        relaunch the SAME account (no rotation — Anthropic infra throttling, not
        this account's usage limit). Returns True if a retry was launched.

        The attempt tally lives in self._transient_attempts (not _launch_params,
        which launch() overwrites) so it survives the relaunch; it is cleared on
        a non-transient failure, on exhaustion, and on a clean turn.
        """
        try:
            from backend.config import settings as _settings
            if not getattr(_settings, "transient_retry_enabled", True):
                return False

            from backend.services.claude_pool import (
                is_transient_overload, transient_retry_delay,
                collect_process_output_for_detection,
            )

            log_contents = await self.get_recent_log_contents(task_id, limit=10)
            combined = collect_process_output_for_detection(stderr_text, log_contents)
            if not is_transient_overload(combined):
                # Non-transient failure — reset tally so the next genuine
                # overload chain starts fresh.
                self._transient_attempts.pop(instance_id, None)
                return False

            attempt = self._transient_attempts.get(instance_id, 0) + 1
            if attempt > _settings.transient_retry_max:
                logger.warning(
                    "Chat task %d transient retries exhausted (%d) — failing turn",
                    task_id, _settings.transient_retry_max,
                )
                self._transient_attempts.pop(instance_id, None)
                return False

            params = self._launch_params.get(instance_id)
            if not params:
                return False

            async with self.db_factory() as db:
                task = await db.get(Task, task_id)
                if not task or not task.session_id:
                    return False
                session_id = task.session_id
                cwd = task.last_cwd or task.target_repo

            config_dir = self._config_dirs.get(instance_id)
            delay = transient_retry_delay(
                attempt,
                _settings.transient_retry_base_delay,
                _settings.transient_retry_max_delay,
            )
            self._transient_attempts[instance_id] = attempt

            logger.info(
                "Chat task %d transient 429/overload — waiting %.0fs before retry #%d/%d",
                task_id, delay, attempt, _settings.transient_retry_max,
            )
            await self.broadcaster.broadcast(f"task:{task_id}", {
                "event_type": "transient_retry",
                "task_id": task_id,
                "attempt": attempt,
                "max_attempts": _settings.transient_retry_max,
                "delay": round(delay, 1),
            })
            await asyncio.sleep(delay)

            await self.launch(
                instance_id=instance_id,
                prompt=params.get("prompt", "请继续之前的工作。"),
                task_id=task_id,
                cwd=cwd,
                model=params.get("model"),
                resume_session_id=session_id,
                git_env=params.get("git_env"),
                thinking_budget=params.get("thinking_budget"),
                effort_level=params.get("effort_level"),
                chat_initiated=True,
                config_dir=config_dir,
                enable_workflows=params.get("enable_workflows", False),
                enabled_skills=params.get("enabled_skills"),
            )
            return True

        except Exception:
            logger.exception("Chat transient retry failed for task %d", task_id)
            self._transient_attempts.pop(instance_id, None)
            return False

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
                # Default-account launch — still rotatable (see dispatcher)
                old_config_dir = os.path.expanduser("~/.claude")

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

            # The session may have been created under a different account dir
            # than the one this instance launched with — locate it
            source_dir = dispatcher.pool.locate_session_config_dir(session_id) or old_config_dir
            migrate_session(
                old_config_dir=source_dir,
                new_config_dir=new_config_dir,
                session_id=session_id,
            )

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

        # Native sub-agent lifecycle (model-spawned Agent/Monitor, observed by
        # the PTY layer) — register into the generic sub-agent tables so the
        # 前端徽章/面板 shows them next to $monitor sessions.
        if task_id and event.get("subagent") and event["event_type"].startswith("subagent_"):
            try:
                await self._upsert_native_sub_agent(
                    task_id, event["event_type"], event["subagent"]
                )
            except Exception:
                logger.exception(
                    "Failed to upsert native sub-agent for task %s", task_id
                )
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

        # Reactivate completed task if sub-agent produces new output
        if (
            task_id
            and event.get("role") == "assistant"
            and event["event_type"] in ("message", "tool_use")
        ):
            async with self.db_factory() as db:
                task = await db.get(Task, task_id)
                if task and task.status == "completed":
                    task.status = "executing"
                    await db.commit()
                    await self.broadcaster.broadcast("tasks", {
                        "event": "status_change",
                        "task_id": task_id,
                        "new_status": "executing",
                    })

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

        # Per-turn transient-overload detection: a server-side 429/overload
        # surfaces as an is_error message ("Server is temporarily limiting
        # requests (not your usage limit)" / overloaded). Flag it so the host
        # can wait + retry even in PTY mode (where the aborted turn still
        # reports exit_code 0).
        #
        # Only the CURRENT foreground turn's own events count. `orphan` events
        # are stale backlog from a previous turn — on resume PTY re-reads the
        # JSONL and replays the very api_error that triggered THIS retry — and
        # `autonomous` events belong to background sub-agent turns. Flagging
        # either keeps transient_error_seen() True across a clean resume, so the
        # host "retries" a turn that already succeeded and finally marks the
        # task failed (the recover-then-failed bug). See PROGRESS.md.
        if (
            event.get("is_error")
            and not event.get("orphan")
            and not event.get("autonomous")
        ):
            from backend.services.claude_pool import is_transient_overload
            if is_transient_overload(event.get("content") or ""):
                self._transient_seen.add(instance_id)

        # Mark task as unread when assistant produces a message or result
        if task_id and event.get("role") == "assistant" and event["event_type"] in ("message", "result"):
            async with self.db_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(has_unread=True)
                )
                await db.commit()

        # Track last tool_use name for evolution (tool_result may not carry tool_name)
        if event["event_type"] == "tool_use" and event.get("tool_name"):
            self._last_tool_name = event["tool_name"]

        # Skill evolution: learn from tool failures
        if (
            task_id
            and event["event_type"] == "tool_result"
            and event.get("is_error")
        ):
            failed_tool = event.get("tool_name") or getattr(self, "_last_tool_name", None)
            if failed_tool:
                try:
                    from backend.services.skill_evolution import evolve_on_failure
                    async with self.db_factory() as db:
                        await evolve_on_failure(
                            tool_name=failed_tool,
                            error=str(event.get("content") or event.get("tool_output", ""))[:500],
                            context=str(event.get("tool_input", ""))[:300],
                            db=db,
                        )
                except Exception:
                    logger.debug("skill evolution failed", exc_info=True)

        # Broadcast via WebSocket
        broadcast_data = {k: v for k, v in event.items() if k != "raw_json"}
        if loop_iteration is not None:
            broadcast_data["loop_iteration"] = loop_iteration
        await self.broadcaster.broadcast(f"instance:{instance_id}", broadcast_data)
        if task_id:
            await self.broadcaster.broadcast(f"task:{task_id}", broadcast_data)

        # Persist and broadcast context usage
        def _model_context_window(model_name: str) -> int:
            # fable 系与 [1m] 变体为 1M 窗口，其余 200K
            m = (model_name or "").lower()
            return 1_000_000 if ("[1m]" in m or "fable" in m) else 200_000

        if context_usage and "total_input_tokens" not in context_usage:
            # Window-only refinement (result events carry just the
            # authoritative contextWindow — their usage numbers are cumulative
            # and unusable). Merge into the stored per-request usage.
            window = context_usage.get("context_window")
            context_usage = None
            if window and task_id:
                async with self.db_factory() as db:
                    t = await db.get(Task, task_id)
                    stored = dict(t.context_window_usage) if (t and t.context_window_usage) else None
                    model_name = (t.model or "") if t else ""
                # modelUsage 上报的窗口对大上下文模型（fable）会低报 200K，
                # 取上报值与模型启发式的较大者
                window = max(window, _model_context_window(model_name))
                if stored and stored.get("context_window") != window:
                    stored["context_window"] = window
                    context_usage = stored
        elif context_usage and not context_usage.get("context_window"):
            # Per-request usage without a window (PTY interactive mode and -p
            # assistant events). Fill from the task's model choice: [1m]
            # variants get 1M, else 200K.
            model_name = ""
            if task_id:
                async with self.db_factory() as db:
                    t = await db.get(Task, task_id)
                    model_name = (t.model or "") if t else ""
            context_usage["context_window"] = _model_context_window(model_name)
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

    async def _upsert_native_sub_agent(
        self, task_id: int, event_type: str, info: dict
    ) -> None:
        """Mirror a native sub-agent lifecycle event into sub_agent_sessions.

        Keyed by tool_use_id (stored in meta JSON): spawn inserts a running
        record, progress bumps checks_done/last_summary, done completes it.
        Broadcasts sub_agent_* WebSocket events for the frontend panel/badge.
        """
        import json as _json
        from sqlalchemy import select as _select
        from backend.models.sub_agent import SubAgentSession

        tool_use_id = info.get("tool_use_id")
        if not tool_use_id:
            return

        async with self.db_factory() as db:
            existing = (
                await db.execute(
                    _select(SubAgentSession).where(
                        SubAgentSession.task_id == task_id,
                        SubAgentSession.source == "native",
                        SubAgentSession.meta.like(f'%"{tool_use_id}"%'),
                    )
                )
            ).scalars().first()

            if event_type == "subagent_spawn":
                if existing:
                    return  # replay safety
                sa = SubAgentSession(
                    task_id=task_id,
                    agent_type=info.get("kind") or "native-agent",
                    source="native",
                    description=(info.get("description") or "")[:500],
                    status="running",
                    meta=_json.dumps(info, ensure_ascii=False),
                )
                db.add(sa)
                await db.commit()
                await db.refresh(sa)
                await self.broadcaster.broadcast(f"task:{task_id}", {
                    "event_type": "sub_agent_session_created",
                    "sub_agent_session_id": sa.id,
                    "agent_type": sa.agent_type,
                    "source": "native",
                    "description": sa.description,
                })
                return

            if not existing:
                return

            if event_type == "subagent_progress":
                existing.checks_done = (existing.checks_done or 0) + 1
                if info.get("summary"):
                    existing.last_summary = info["summary"][:2000]
                await db.commit()
                await self.broadcaster.broadcast(f"task:{task_id}", {
                    "event_type": "sub_agent_report",
                    "sub_agent_session_id": existing.id,
                    "agent_type": existing.agent_type,
                    "check_number": existing.checks_done,
                    "summary": existing.last_summary,
                })
            elif event_type == "subagent_done":
                existing.status = "completed"
                existing.completed_at = datetime.utcnow()
                if info.get("timed_out"):
                    existing.last_summary = (
                        (existing.last_summary or "") + " [timed out]"
                    ).strip()
                await db.commit()
                await self.broadcaster.broadcast(f"task:{task_id}", {
                    "event_type": "sub_agent_session_status",
                    "sub_agent_session_id": existing.id,
                    "agent_type": existing.agent_type,
                    "status": "completed",
                })

                # Auto-resume: native monitor 完成后通知主 Agent
                if existing.agent_type in ("native-monitor",):
                    try:
                        from backend.main import dispatcher
                        from backend.services.dispatcher import PRIORITY_MONITOR_COMPLETE
                        summary = existing.last_summary or existing.description or "监控完成"
                        prompt = (
                            f"[Native Monitor 完成] {summary}\n\n"
                            "请根据监控结果决定下一步操作。"
                        )
                        await dispatcher.enqueue_message(
                            task_id=task_id,
                            prompt=prompt,
                            priority=PRIORITY_MONITOR_COMPLETE,
                            source="monitor:native-complete",
                            user_message_text=f"[Monitor] 监控完成: {summary}",
                        )
                    except Exception:
                        logger.exception(
                            "Failed to enqueue auto-resume for native monitor on task %s",
                            task_id,
                        )

    # ---------------------------------------------------- PTY 权限透传

    _PTY_PERMISSION_TIMEOUT = 120  # channel server 阻塞上限（秒），超时 deny

    def _on_pty_permission_request(self, session_id: str, request: dict) -> None:
        """BridgeHub HTTP 线程回调——只做线程切换，业务在事件循环里处理。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning(
                "PTY permission request dropped (no event loop): %s", request
            )
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_pty_permission_request(session_id, request), loop
        )

    async def _handle_pty_permission_request(
        self, session_id: str, request: dict
    ) -> None:
        """把 CC 的权限请求落库并广播成前端聊天卡片。"""
        import json as _json
        import time as _time

        request_id = request.get("request_id")
        if not request_id:
            return

        task_id = None
        async with self.db_factory() as db:
            row = (
                await db.execute(
                    select(Task)
                    .where(Task.session_id == session_id)
                    .order_by(Task.id.desc())
                )
            ).scalars().first()
            if row:
                task_id = row.id

        self._pty_permissions[request_id] = {
            "session_id": session_id,
            "task_id": task_id,
            "tool_name": request.get("tool_name"),
            "expires_at": _time.monotonic() + self._PTY_PERMISSION_TIMEOUT,
        }

        payload = {
            "event_type": "permission_request",
            "request_id": request_id,
            "tool_name": request.get("tool_name"),
            "description": request.get("description"),
            "input_preview": request.get("input_preview"),
            "timeout_seconds": self._PTY_PERMISSION_TIMEOUT,
        }

        if task_id:
            instance_id = row.instance_id or 1
            async with self.db_factory() as db:
                db.add(LogEntry(
                    instance_id=instance_id,
                    task_id=task_id,
                    event_type="permission_request",
                    role="system",
                    content=request.get("description")
                    or f"权限请求: {request.get('tool_name')}",
                    tool_name=request.get("tool_name"),
                    tool_input=request.get("input_preview"),
                    raw_json=_json.dumps(
                        {"request_id": request_id, "session_id": session_id},
                        ensure_ascii=False,
                    ),
                ))
                await db.commit()
            await self.broadcaster.broadcast(f"task:{task_id}", payload)
        else:
            logger.warning(
                "PTY permission request for unknown session %s (tool=%s)",
                session_id, request.get("tool_name"),
            )

    async def resolve_pty_permission(self, request_id: str, behavior: str) -> bool:
        """前端按钮回包 → BridgeHub → channel server 解除阻塞。

        Returns False when the request is unknown/expired（channel server
        已超时默认 deny）。
        """
        import json as _json
        import time as _time

        pending = self._pty_permissions.pop(request_id, None)
        # 顺手清理其他过期项
        now = _time.monotonic()
        for rid in [r for r, p in self._pty_permissions.items()
                    if p["expires_at"] < now]:
            self._pty_permissions.pop(rid, None)

        if not pending or pending["expires_at"] < now:
            return False
        if self._pty_backend is None:
            return False

        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None,
            self._pty_backend._bridge.resolve_permission,
            pending["session_id"],
            request_id,
            behavior,
        )

        # 只有真正送达 CC（channel server 还挂着这个请求）才记录/广播，
        # 否则其他在线客户端会把过期请求误标成"已允许/拒绝"
        task_id = pending.get("task_id")
        if ok and task_id:
            async with self.db_factory() as db:
                db.add(LogEntry(
                    instance_id=1,
                    task_id=task_id,
                    event_type="system_event",
                    role="system",
                    content=f"permission_{behavior}: {pending.get('tool_name')}",
                    raw_json=_json.dumps({"request_id": request_id}),
                ))
                await db.commit()
            await self.broadcaster.broadcast(f"task:{task_id}", {
                "event_type": "permission_resolved",
                "request_id": request_id,
                "behavior": behavior,
            })
        return bool(ok)

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
        self._transient_attempts.pop(instance_id, None)
        self._stopping.discard(instance_id)
        return True

    def is_running(self, instance_id: int) -> bool:
        process = self.processes.get(instance_id)
        return process is not None and process.returncode is None

    def get_last_stderr(self, instance_id: int) -> str:
        return self._last_stderr.pop(instance_id, "")

    def get_config_dir(self, instance_id: int) -> str | None:
        return self._config_dirs.get(instance_id)

    def transient_error_seen(self, instance_id: int) -> bool:
        """True if the instance's most recent turn emitted a transient
        server-side 429/overload error (turn-scoped; reset at next launch)."""
        return instance_id in self._transient_seen

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
