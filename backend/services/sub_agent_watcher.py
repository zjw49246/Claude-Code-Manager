"""Background watcher for native sub-agents.

Periodically polls transcript files of running native sub-agents
(spawned by Claude Code's Agent tool with run_in_background=true)
to track their progress and detect completion — independent of the
main session's event stream.

Similar to how MonitorDispatcher manages $monitor sessions, but for
native background agents whose transcripts live on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)

POLL_INTERVAL = 12  # seconds between transcript checks
IDLE_THRESHOLD = 60  # seconds of no transcript growth → consider completed
MAX_SUMMARY_LEN = 2000


class SubAgentWatcher:
    def __init__(self, db_factory: Callable, broadcaster: WebSocketBroadcaster):
        self.db_factory = db_factory
        self.broadcaster = broadcaster
        self._task: asyncio.Task | None = None
        # session_id -> {last_size, idle_since, agent_id, task_id, jsonl_path}
        self._tracked: dict[int, dict] = {}

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("SubAgentWatcher started")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _poll_loop(self):
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug("SubAgentWatcher tick error", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self):
        from sqlalchemy import select
        from backend.models.sub_agent import SubAgentSession, SubAgentReport
        from backend.models.task import Task
        from backend.models.log_entry import LogEntry

        async with self.db_factory() as db:
            result = await db.execute(
                select(SubAgentSession).where(
                    SubAgentSession.source == "native",
                    SubAgentSession.status == "running",
                    SubAgentSession.agent_type.in_(["native-agent", "native-monitor"]),
                )
            )
            running = result.scalars().all()

        if not running:
            self._tracked.clear()
            return

        for sa in running:
            sid = sa.id
            if sid not in self._tracked:
                # Resolve transcript path from meta
                info = self._resolve_paths(sa)
                if not info:
                    continue
                self._tracked[sid] = {
                    "task_id": sa.task_id,
                    "agent_id": info["agent_id"],
                    "jsonl_path": info["jsonl_path"],
                    "last_size": 0,
                    "idle_since": None,
                    "description": sa.description,
                }

            tracked = self._tracked.get(sid)
            if not tracked:
                continue

            jsonl_path = tracked["jsonl_path"]
            if not os.path.exists(jsonl_path):
                continue

            try:
                current_size = os.path.getsize(jsonl_path)
            except OSError:
                continue

            if current_size > tracked["last_size"]:
                # Transcript grew — read new content for progress
                summary = self._read_latest_summary(jsonl_path, tracked["last_size"])
                tracked["last_size"] = current_size
                tracked["idle_since"] = None

                if summary:
                    async with self.db_factory() as db:
                        sa_obj = await db.get(SubAgentSession, sid)
                        if not sa_obj or sa_obj.status != "running":
                            continue
                        sa_obj.checks_done = (sa_obj.checks_done or 0) + 1
                        sa_obj.last_summary = summary[:MAX_SUMMARY_LEN]

                        db.add(SubAgentReport(
                            session_id=sid,
                            check_number=sa_obj.checks_done,
                            status="running",
                            summary=summary[:MAX_SUMMARY_LEN],
                        ))

                        db.add(LogEntry(
                            instance_id=None,
                            task_id=tracked["task_id"],
                            event_type="system_event",
                            content=f"[Agent #{sid}] {tracked['description']}: {summary[:300]}",
                            is_error=False,
                        ))
                        await db.commit()

                    await self.broadcaster.broadcast(f"task:{tracked['task_id']}", {
                        "event_type": "sub_agent_report",
                        "sub_agent_session_id": sid,
                        "agent_type": "native-agent",
                        "check_number": sa_obj.checks_done if sa_obj else 1,
                        "summary": summary[:MAX_SUMMARY_LEN],
                    })
            else:
                # No growth
                if tracked["idle_since"] is None:
                    tracked["idle_since"] = datetime.utcnow()
                else:
                    idle_secs = (datetime.utcnow() - tracked["idle_since"]).total_seconds()
                    if idle_secs >= IDLE_THRESHOLD:
                        # Check if the process is still alive
                        if not self._process_alive(tracked["agent_id"], sa.task_id):
                            await self._mark_completed(sid, tracked)

        # Clean up tracked entries for sessions no longer running
        running_ids = {sa.id for sa in running}
        for sid in list(self._tracked):
            if sid not in running_ids:
                del self._tracked[sid]

    def _resolve_paths(self, sa) -> dict | None:
        """Resolve agent_id and transcript path from sub-agent meta + task session."""
        try:
            meta = json.loads(sa.meta) if sa.meta else {}
        except (json.JSONDecodeError, TypeError):
            return None

        tool_use_id = meta.get("tool_use_id")
        if not tool_use_id:
            return None

        # Find session directory from task
        import sqlite3
        from backend.config import settings
        db_url = settings.database_url
        if "sqlite" not in db_url:
            return None
        raw = db_url.split("///", 1)[-1] if "///" in db_url else db_url
        conn = sqlite3.connect(raw)
        try:
            row = conn.execute(
                "SELECT session_id FROM tasks WHERE id = ?", (sa.task_id,)
            ).fetchone()
        finally:
            conn.close()

        if not row or not row[0]:
            return None
        session_id = row[0]

        # Search for subagents directory in claude config dirs
        for config_dir in Path.home().glob(".claude-account-*"):
            projects_dir = config_dir / "projects"
            if not projects_dir.exists():
                continue
            for project_dir in projects_dir.iterdir():
                sa_dir = project_dir / session_id / "subagents"
                if not sa_dir.exists():
                    continue
                # Find meta.json matching tool_use_id
                for meta_file in sa_dir.glob("*.meta.json"):
                    try:
                        with open(meta_file) as f:
                            file_meta = json.load(f)
                        if file_meta.get("toolUseId", "").startswith(tool_use_id[:20]):
                            agent_id = meta_file.name.replace(".meta.json", "").replace("agent-", "")
                            jsonl_path = str(sa_dir / f"agent-{agent_id}.jsonl")
                            return {"agent_id": agent_id, "jsonl_path": jsonl_path}
                    except (OSError, json.JSONDecodeError):
                        continue
        return None

    def _read_latest_summary(self, jsonl_path: str, from_offset: int) -> str | None:
        """Read new lines from transcript and extract the latest assistant text."""
        try:
            last_text = ""
            with open(jsonl_path, encoding="utf-8") as f:
                f.seek(max(0, from_offset))
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        msg = raw.get("message", {})
                        if raw.get("type") in ("assistant", "message"):
                            for block in (msg.get("content") or []):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    last_text = block["text"]
                                elif isinstance(block, dict) and block.get("type") == "tool_use":
                                    last_text = f"[{block.get('name', 'tool')}]"
                    except json.JSONDecodeError:
                        continue
            return last_text[:MAX_SUMMARY_LEN] if last_text else None
        except OSError:
            return None

    def _process_alive(self, agent_id: str, task_id: int) -> bool:
        """Check if the sub-agent process is likely still running.

        Uses file modification time as proxy — if the transcript hasn't
        been modified for longer than IDLE_THRESHOLD, the process is gone.
        """
        tracked = None
        for t in self._tracked.values():
            if t.get("agent_id") == agent_id:
                tracked = t
                break
        if not tracked:
            return False
        try:
            mtime = os.path.getmtime(tracked["jsonl_path"])
            age = (datetime.utcnow() - datetime.utcfromtimestamp(mtime)).total_seconds()
            return age < IDLE_THRESHOLD
        except OSError:
            return False

    async def _mark_completed(self, sid: int, tracked: dict):
        """Mark a sub-agent as completed."""
        from backend.models.sub_agent import SubAgentSession

        # Read final summary
        final_summary = self._read_latest_summary(tracked["jsonl_path"], 0)

        async with self.db_factory() as db:
            sa = await db.get(SubAgentSession, sid)
            if not sa or sa.status != "running":
                return
            sa.status = "completed"
            sa.completed_at = datetime.utcnow()
            if final_summary:
                sa.last_summary = final_summary[:MAX_SUMMARY_LEN]
            await db.commit()

        await self.broadcaster.broadcast(f"task:{tracked['task_id']}", {
            "event_type": "sub_agent_session_status",
            "sub_agent_session_id": sid,
            "agent_type": "native-agent",
            "status": "completed",
        })

        if sid in self._tracked:
            del self._tracked[sid]

        logger.info("SubAgentWatcher: marked SA %d as completed", sid)
