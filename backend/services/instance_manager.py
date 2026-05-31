import asyncio
import logging
import os
import re
import signal
from datetime import datetime

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

    async def launch(self, instance_id: int, prompt: str, task_id: int | None = None, cwd: str | None = None, model: str | None = None, resume_session_id: str | None = None, loop_iteration: int | None = None, git_env: dict | None = None, thinking_budget: int | None = None, effort_level: str | None = None, chat_initiated: bool = False, config_dir: str | None = None) -> int:
        """Launch a Claude Code subprocess for the given instance.

        If resume_session_id is provided, uses --resume to continue the conversation.
        loop_iteration is recorded on every LogEntry produced by this invocation so
        that loop-task chat history can be grouped by iteration in the frontend.
        """
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

        # Must unset CLAUDE_CODE env var to avoid nested session detection
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")}

        # Inject per-project git identity and credentials as environment variables.
        # These take precedence over any global ~/.gitconfig or system credential helper.
        if git_env:
            env.update(git_env)

        # Pool: inject CLAUDE_CONFIG_DIR so this subprocess uses a specific account
        if config_dir:
            env["CLAUDE_CONFIG_DIR"] = config_dir
            self._config_dirs[instance_id] = config_dir

        # Forward Extended Thinking budget. Claude Code reads MAX_THINKING_TOKENS
        # to decide the per-turn thinking budget. Skip when 0 / negative / None.
        if thinking_budget and thinking_budget > 0:
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
            self._consume_output(instance_id, task_id, process, loop_iteration, chat_initiated)
        )
        self._tasks[instance_id] = consumer

        return process.pid

    async def _consume_output(self, instance_id: int, task_id: int | None, process: asyncio.subprocess.Process, loop_iteration: int | None = None, chat_initiated: bool = False):
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

                    events = self.parser.parse_line(text)
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
                    if exit_code == 0 or interrupted:
                        result = await db.execute(
                            update(Task)
                            .where(Task.id == task_id, Task.status == "executing")
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
                            .where(Task.id == task_id, Task.status == "executing")
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
