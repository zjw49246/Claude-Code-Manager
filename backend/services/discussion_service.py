"""Discussion service: Facilitator-driven multi-agent discussion."""
import asyncio
import json
import logging
import os
import signal
import tempfile
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models.discussion import (
    Discussion,
    DiscussionAgent,
    DiscussionEvent,
    DiscussionMessage,
)
from backend.services.stream_parser import StreamParser
from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)

MAX_AUTO_ROUNDS = 10


class DiscussionService:
    def __init__(self, db_factory, broadcaster: WebSocketBroadcaster):
        self.db_factory = db_factory
        self.broadcaster = broadcaster
        self.parser = StreamParser()
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        self._consumers: dict[int, asyncio.Task] = {}
        self._facilitator_locks: dict[int, asyncio.Lock] = {}
        self._round_count: dict[int, int] = {}

    def _get_lock(self, discussion_id: int) -> asyncio.Lock:
        if discussion_id not in self._facilitator_locks:
            self._facilitator_locks[discussion_id] = asyncio.Lock()
        return self._facilitator_locks[discussion_id]

    # ------------------------------------------------------------------
    # Public: user sends group message
    # ------------------------------------------------------------------
    async def send_broadcast(
        self,
        db: AsyncSession,
        discussion_id: int,
        user_message: str,
    ) -> list[DiscussionAgent]:
        disc = await db.get(Discussion, discussion_id)
        if not disc:
            raise ValueError(f"Discussion {discussion_id} not found")

        user_msg = DiscussionMessage(
            discussion_id=discussion_id,
            role="user",
            content=user_message,
            created_at=datetime.now(timezone.utc),
        )
        db.add(user_msg)
        await db.commit()

        await self.broadcaster.broadcast(f"discussion:{discussion_id}", {
            "event_type": "discussion_message",
            "message": _msg_to_dict(user_msg),
        })

        existing = await db.execute(
            select(DiscussionAgent).where(
                DiscussionAgent.discussion_id == discussion_id
            )
        )
        existing_agents = list(existing.scalars().all())

        self._round_count[discussion_id] = 0

        if existing_agents:
            asyncio.get_event_loop().create_task(
                self._facilitator_advance(discussion_id)
            )
            return existing_agents

        history = await self._get_history(db, discussion_id)
        history_file = self._write_history_file(discussion_id, history)

        roles = await self._run_facilitator_init(disc, history_file)

        agents = []
        for role in roles:
            agent = DiscussionAgent(
                discussion_id=discussion_id,
                role_name=role["role_name"],
                system_prompt=role["system_prompt"],
                status="running",
                created_at=datetime.now(timezone.utc),
            )
            db.add(agent)
            await db.flush()
            agents.append(agent)

            await self.broadcaster.broadcast(f"discussion:{discussion_id}", {
                "event_type": "agent_spawned",
                "agent": _agent_to_dict(agent),
            })

        await db.commit()

        for agent in agents:
            self._launch_agent(agent, disc, history_file)

        return agents

    # ------------------------------------------------------------------
    # Public: send message to a specific agent (resume session)
    # ------------------------------------------------------------------
    async def send_to_agent(
        self,
        db: AsyncSession,
        agent_id: int,
        message: str,
    ) -> None:
        agent = await db.get(DiscussionAgent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        if agent.status == "running":
            raise ValueError(f"Agent {agent_id} is already running")

        disc = await db.get(Discussion, agent.discussion_id)
        if not disc:
            raise ValueError(f"Discussion {agent.discussion_id} not found")

        user_evt = DiscussionEvent(
            discussion_id=agent.discussion_id,
            agent_id=agent_id,
            event_type="user_message",
            role="user",
            content=message,
            timestamp=datetime.now(timezone.utc),
        )
        db.add(user_evt)

        agent.status = "running"
        await db.commit()

        await self.broadcaster.broadcast(
            f"discussion:{agent.discussion_id}:agent:{agent_id}",
            {
                "event_type": "user_message",
                "role": "user",
                "content": message,
                "agent_id": agent_id,
            },
        )
        await self.broadcaster.broadcast(f"discussion:{agent.discussion_id}", {
            "event_type": "agent_status",
            "agent_id": agent_id,
            "status": "running",
        })

        self._launch_agent_resume(agent, disc, message)

    # ------------------------------------------------------------------
    # Public: trigger an idle agent
    # ------------------------------------------------------------------
    async def trigger_agent(
        self,
        db: AsyncSession,
        agent_id: int,
    ) -> None:
        agent = await db.get(DiscussionAgent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        if agent.status == "running":
            raise ValueError(f"Agent {agent_id} is already running")

        disc = await db.get(Discussion, agent.discussion_id)
        if not disc:
            raise ValueError(f"Discussion not found")

        history = await self._get_history(db, agent.discussion_id)
        history_file = self._write_history_file(agent.discussion_id, history)

        agent.status = "running"
        await db.commit()

        await self.broadcaster.broadcast(f"discussion:{agent.discussion_id}", {
            "event_type": "agent_status",
            "agent_id": agent_id,
            "status": "running",
        })

        prompt = f"""\
{agent.system_prompt}

The discussion has continued since your last response.
Updated discussion history is at: {history_file}
Read it, then provide your updated analysis from your perspective as "{agent.role_name}".
Write in Chinese."""

        self._launch_agent_with_prompt(agent, disc, prompt, history_file)

    # ------------------------------------------------------------------
    # Public: stop a running agent
    # ------------------------------------------------------------------
    async def stop_agent(self, agent_id: int) -> None:
        process = self._processes.get(agent_id)
        if process and process.returncode is None:
            try:
                process.send_signal(signal.SIGINT)
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()

    # ------------------------------------------------------------------
    # Public: add one more agent via facilitator
    # ------------------------------------------------------------------
    async def add_agent(
        self,
        db: AsyncSession,
        discussion_id: int,
    ) -> DiscussionAgent:
        disc = await db.get(Discussion, discussion_id)
        if not disc:
            raise ValueError(f"Discussion {discussion_id} not found")

        existing_agents = await db.execute(
            select(DiscussionAgent).where(DiscussionAgent.discussion_id == discussion_id)
        )
        existing_roles = [a.role_name for a in existing_agents.scalars().all()]

        history = await self._get_history(db, discussion_id)
        history_file = self._write_history_file(discussion_id, history)

        role = await self._run_facilitator_add_one(disc, history_file, existing_roles)

        agent = DiscussionAgent(
            discussion_id=discussion_id,
            role_name=role["role_name"],
            system_prompt=role["system_prompt"],
            status="running",
            created_at=datetime.now(timezone.utc),
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        await self.broadcaster.broadcast(f"discussion:{discussion_id}", {
            "event_type": "agent_spawned",
            "agent": _agent_to_dict(agent),
        })

        self._launch_agent(agent, disc, history_file)
        return agent

    # ------------------------------------------------------------------
    # Facilitator auto-advance: triggered when an agent finishes
    # ------------------------------------------------------------------
    async def _maybe_auto_advance(self, discussion_id: int) -> None:
        """Check if all agents are idle; if so, trigger facilitator."""
        async with self.db_factory() as db:
            result = await db.execute(
                select(DiscussionAgent).where(
                    DiscussionAgent.discussion_id == discussion_id
                )
            )
            agents = list(result.scalars().all())

        if not agents:
            return
        if any(a.status == "running" for a in agents):
            return

        round_num = self._round_count.get(discussion_id, 0)
        if round_num >= MAX_AUTO_ROUNDS:
            logger.info(
                "Discussion %s reached max auto-advance rounds (%d), stopping",
                discussion_id, MAX_AUTO_ROUNDS,
            )
            return

        await self._facilitator_advance(discussion_id)

    async def _facilitator_advance(self, discussion_id: int) -> None:
        """Facilitator analyzes current state and decides next steps."""
        lock = self._get_lock(discussion_id)
        if lock.locked():
            return

        async with lock:
            async with self.db_factory() as db:
                disc = await db.get(Discussion, discussion_id)
                if not disc:
                    return

                agents = await db.execute(
                    select(DiscussionAgent).where(
                        DiscussionAgent.discussion_id == discussion_id
                    )
                )
                agent_list = list(agents.scalars().all())
                if not agent_list:
                    return

                agent_summaries = await self._collect_agent_summaries(db, discussion_id)
                messages = await self._get_history(db, discussion_id)

            goal = messages[0].content if messages else disc.title
            round_num = self._round_count.get(discussion_id, 0) + 1
            self._round_count[discussion_id] = round_num

            decision = await self._run_facilitator_decide(
                disc, goal, agent_summaries, round_num
            )

            action = decision.get("action", "complete")

            if action == "complete":
                final = decision.get("final_output", "")
                if final:
                    async with self.db_factory() as db:
                        msg = DiscussionMessage(
                            discussion_id=discussion_id,
                            role="facilitator",
                            agent_role_name="Facilitator",
                            content=final,
                            created_at=datetime.now(timezone.utc),
                        )
                        db.add(msg)
                        await db.commit()

                    await self.broadcaster.broadcast(
                        f"discussion:{discussion_id}",
                        {
                            "event_type": "discussion_message",
                            "message": {
                                "id": -1,
                                "discussion_id": discussion_id,
                                "role": "facilitator",
                                "agent_role_name": "Facilitator",
                                "content": final,
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            },
                        },
                    )
                return

            instructions = decision.get("instructions", {})
            if not instructions:
                return

            async with self.db_factory() as db:
                disc = await db.get(Discussion, discussion_id)
                agents_result = await db.execute(
                    select(DiscussionAgent).where(
                        DiscussionAgent.discussion_id == discussion_id
                    )
                )
                all_agents = {a.role_name: a for a in agents_result.scalars().all()}

                for role_name, instruction in instructions.items():
                    agent = all_agents.get(role_name)
                    if not agent or agent.status == "running":
                        continue

                    agent.status = "running"
                    await db.commit()

                    await self.broadcaster.broadcast(
                        f"discussion:{discussion_id}",
                        {
                            "event_type": "agent_status",
                            "agent_id": agent.id,
                            "status": "running",
                        },
                    )

                    if agent.session_id:
                        self._launch_agent_resume(agent, disc, instruction)
                    else:
                        full_prompt = f"{agent.system_prompt}\n\n{instruction}"
                        self._launch_agent_with_prompt(agent, disc, full_prompt)

    async def _run_facilitator_decide(
        self,
        disc: Discussion,
        goal: str,
        agent_summaries: dict[str, str],
        round_num: int,
    ) -> dict:
        """Facilitator decides: continue with instructions, or complete."""
        summaries_text = "\n\n".join(
            f"### {name}\n{summary}" for name, summary in agent_summaries.items()
        )
        agent_names = ", ".join(agent_summaries.keys())

        prompt = f"""\
你是一场多角色讨论的协调者(Facilitator)。

## 讨论目标
{goal}

## 当前参与角色
{agent_names}

## 各角色最新产出
{summaries_text}

## 当前轮次
第 {round_num} 轮（最多 {MAX_AUTO_ROUNDS} 轮）

## 你的任务
分析各角色的产出，判断讨论目标是否已经达成。

如果还需要继续：决定哪些角色需要继续工作，给每个角色**具体的下一步指令**。
指令中要包含其他角色的关键观点（交叉分享），以及你希望该角色接下来重点分析的方向。
不需要所有角色都继续，只让有需要的角色继续。

如果目标已达成：生成最终产出，综合所有角色的分析。

## 输出格式
只输出一个 JSON 对象，不要有其他文字：

继续讨论：
{{
  "action": "continue",
  "reason": "为什么需要继续，当前还缺什么",
  "instructions": {{
    "角色名1": "给该角色的具体指令，包含交叉分享的信息...",
    "角色名2": "给该角色的具体指令..."
  }}
}}

讨论完成：
{{
  "action": "complete",
  "reason": "为什么判断目标已达成",
  "final_output": "最终综合产出（Markdown格式，完整且可交付）"
}}"""

        return await self._run_facilitator_structured(disc, prompt)

    async def _run_facilitator_process(
        self, disc: Discussion, prompt: str
    ) -> list[str]:
        """Run facilitator subprocess, stream events, capture session_id. Returns collected text."""
        env = self._build_env()
        cmd = [
            settings.claude_binary,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            "--model", disc.facilitator_model,
            "--max-turns", "5",
        ]
        if disc.facilitator_session_id:
            cmd.extend(["--resume", disc.facilitator_session_id])
        cmd.extend(["-p", prompt])

        discussion_id = disc.id
        collected_text: list[str] = []
        captured_session_id: str | None = None

        await self.broadcaster.broadcast(f"discussion:{discussion_id}", {
            "event_type": "facilitator_status",
            "status": "running",
        })

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=10 * 1024 * 1024,
            )

            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                events = self.parser.parse_line(text)
                for event in events:
                    sid = event.pop("session_id", None)
                    if sid and not captured_session_id:
                        captured_session_id = sid
                    event.pop("cost_usd", None)
                    event.pop("context_usage", None)
                    et = event.get("event_type", "")

                    if et in ("message", "result") and event.get("content"):
                        collected_text.append(event["content"])

                    await self._save_facilitator_event(discussion_id, et, event)

                    broadcast_data = {
                        k: v for k, v in event.items() if k != "raw_json"
                    }
                    broadcast_data["event_type"] = (
                        f"facilitator_{et}" if et else "facilitator_unknown"
                    )
                    await self.broadcaster.broadcast(
                        f"discussion:{discussion_id}", broadcast_data
                    )

            await process.wait()

        except Exception as e:
            logger.exception("Facilitator process failed")
            await self.broadcaster.broadcast(f"discussion:{discussion_id}", {
                "event_type": "facilitator_status",
                "status": "error",
                "error": str(e),
            })
            raise

        if captured_session_id:
            async with self.db_factory() as db:
                await db.execute(
                    update(Discussion)
                    .where(Discussion.id == discussion_id)
                    .values(facilitator_session_id=captured_session_id)
                )
                await db.commit()
            disc.facilitator_session_id = captured_session_id

        await self.broadcaster.broadcast(f"discussion:{discussion_id}", {
            "event_type": "facilitator_status",
            "status": "done",
        })

        return collected_text

    async def _run_facilitator_structured(
        self, disc: Discussion, prompt: str
    ) -> dict:
        """Run facilitator and parse structured JSON response."""
        try:
            collected_text = await self._run_facilitator_process(disc, prompt)
        except Exception as e:
            return {"action": "complete", "reason": f"Facilitator error: {e}"}

        raw = "\n".join(collected_text).strip()
        return self._parse_json_response(raw)

    def _parse_json_response(self, raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
            if isinstance(data, dict) and "action" in data:
                return data
        except (json.JSONDecodeError, TypeError):
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, dict) and "action" in data:
                    return data
            except (json.JSONDecodeError, TypeError):
                pass

        logger.warning("Could not parse facilitator decision, defaulting to complete. Raw: %s", text[:300])
        return {
            "action": "complete",
            "reason": "无法解析协调者输出",
            "final_output": text if text else "讨论未能产生结构化结论。",
        }

    # ------------------------------------------------------------------
    # Internal: launch agent subprocess
    # ------------------------------------------------------------------
    def _launch_agent(
        self,
        agent: DiscussionAgent,
        disc: Discussion,
        history_file: str,
    ) -> None:
        prompt = f"""\
{agent.system_prompt}

Discussion history is at: {history_file}
Read it first, then respond from your perspective as "{agent.role_name}".

Guidelines:
- Be specific and actionable, not generic
- If you disagree with another participant, say so directly and explain why
- Write in Chinese"""

        self._launch_agent_with_prompt(agent, disc, prompt, history_file)

    def _launch_agent_resume(
        self,
        agent: DiscussionAgent,
        disc: Discussion,
        message: str,
    ) -> None:
        cmd = self._build_cmd(disc.agent_model, resume_session_id=agent.session_id)
        cmd.extend(["-p", message])

        env = self._build_env()
        cwd = agent.last_cwd

        task = asyncio.get_event_loop().create_task(
            self._run_and_consume(agent.id, agent.discussion_id, cmd, env, cwd)
        )
        self._consumers[agent.id] = task

    def _launch_agent_with_prompt(
        self,
        agent: DiscussionAgent,
        disc: Discussion,
        prompt: str,
        history_file: str | None = None,
    ) -> None:
        cmd = self._build_cmd(disc.agent_model, resume_session_id=agent.session_id)
        cmd.extend(["-p", prompt])

        env = self._build_env()

        task = asyncio.get_event_loop().create_task(
            self._run_and_consume(
                agent.id, agent.discussion_id, cmd, env,
                cwd=agent.last_cwd,
                cleanup_file=history_file,
            )
        )
        self._consumers[agent.id] = task

    def _build_cmd(self, model: str, resume_session_id: str | None = None) -> list[str]:
        cmd = [
            settings.claude_binary,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
        ]
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
        return cmd

    def _build_env(self) -> dict:
        return {
            k: v
            for k, v in os.environ.items()
            if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")
        }

    # ------------------------------------------------------------------
    # Internal: run subprocess and consume stream
    # ------------------------------------------------------------------
    async def _run_and_consume(
        self,
        agent_id: int,
        discussion_id: int,
        cmd: list[str],
        env: dict,
        cwd: str | None = None,
        cleanup_file: str | None = None,
    ) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                limit=10 * 1024 * 1024,
            )
            self._processes[agent_id] = process

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
                        for event in events:
                            try:
                                await self._process_event(agent_id, discussion_id, event)
                            except Exception:
                                logger.exception(
                                    "Failed to process event for agent %s: %s",
                                    agent_id, event.get("event_type"),
                                )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Error in consume loop for agent %s", agent_id)
            except asyncio.CancelledError:
                pass
            finally:
                await process.wait()
                exit_code = process.returncode

                stderr_data = await process.stderr.read()
                stderr_text = stderr_data.decode("utf-8", errors="replace").strip() if stderr_data else ""

                new_status = "idle" if exit_code in (0, -2, 130) else "error"

                async with self.db_factory() as db:
                    await db.execute(
                        update(DiscussionAgent)
                        .where(DiscussionAgent.id == agent_id)
                        .values(status=new_status, pid=None)
                    )
                    await db.commit()

                await self.broadcaster.broadcast(
                    f"discussion:{discussion_id}:agent:{agent_id}",
                    {
                        "event_type": "process_exit",
                        "agent_id": agent_id,
                        "exit_code": exit_code,
                        "stderr": stderr_text[:2000] if stderr_text else None,
                    },
                )
                await self.broadcaster.broadcast(f"discussion:{discussion_id}", {
                    "event_type": "agent_status",
                    "agent_id": agent_id,
                    "status": new_status,
                })

                self._processes.pop(agent_id, None)
                self._consumers.pop(agent_id, None)

                if new_status == "idle":
                    asyncio.get_event_loop().create_task(
                        self._maybe_auto_advance(discussion_id)
                    )
        finally:
            if cleanup_file:
                try:
                    os.unlink(cleanup_file)
                except OSError:
                    pass

    async def _process_event(
        self, agent_id: int, discussion_id: int, event: dict
    ) -> None:
        session_id = event.pop("session_id", None)
        event.pop("cost_usd", None)
        event.pop("context_usage", None)

        async with self.db_factory() as db:
            if session_id:
                await db.execute(
                    update(DiscussionAgent)
                    .where(DiscussionAgent.id == agent_id)
                    .values(session_id=session_id)
                )

            evt = DiscussionEvent(
                discussion_id=discussion_id,
                agent_id=agent_id,
                event_type=event.get("event_type", "unknown"),
                role=event.get("role"),
                content=event.get("content"),
                tool_name=event.get("tool_name"),
                tool_input=event.get("tool_input"),
                tool_output=event.get("tool_output"),
                raw_json=event.get("raw_json"),
                is_error=event.get("is_error", False),
                timestamp=datetime.now(timezone.utc),
            )
            db.add(evt)
            await db.commit()

        broadcast_data = {
            k: v for k, v in event.items() if k != "raw_json"
        }
        broadcast_data["agent_id"] = agent_id
        await self.broadcaster.broadcast(
            f"discussion:{discussion_id}:agent:{agent_id}",
            broadcast_data,
        )

    async def _save_facilitator_event(
        self, discussion_id: int, event_type: str, event: dict
    ) -> None:
        async with self.db_factory() as db:
            evt = DiscussionEvent(
                discussion_id=discussion_id,
                agent_id=0,
                event_type=event_type,
                role=event.get("role"),
                content=event.get("content"),
                tool_name=event.get("tool_name"),
                tool_input=event.get("tool_input"),
                tool_output=event.get("tool_output"),
                raw_json=event.get("raw_json"),
                is_error=event.get("is_error", False),
                timestamp=datetime.now(timezone.utc),
            )
            db.add(evt)
            await db.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _collect_agent_summaries(
        self, db: AsyncSession, discussion_id: int
    ) -> dict[str, str]:
        agents_result = await db.execute(
            select(DiscussionAgent).where(
                DiscussionAgent.discussion_id == discussion_id
            )
        )
        summaries: dict[str, str] = {}
        for agent in agents_result.scalars().all():
            events_result = await db.execute(
                select(DiscussionEvent)
                .where(
                    DiscussionEvent.agent_id == agent.id,
                    DiscussionEvent.event_type.in_(["result", "message"]),
                    DiscussionEvent.content.isnot(None),
                    DiscussionEvent.content != "",
                )
                .order_by(DiscussionEvent.id.desc())
                .limit(1)
            )
            last_evt = events_result.scalars().first()
            if last_evt and last_evt.content:
                content = last_evt.content
                if len(content) > 2000:
                    content = content[:2000] + "..."
                summaries[agent.role_name] = content
        return summaries

    async def _get_history(
        self, db: AsyncSession, discussion_id: int
    ) -> list[DiscussionMessage]:
        result = await db.execute(
            select(DiscussionMessage)
            .where(DiscussionMessage.discussion_id == discussion_id)
            .order_by(DiscussionMessage.id)
        )
        return list(result.scalars().all())

    def _write_history_file(
        self, discussion_id: int, messages: list[DiscussionMessage]
    ) -> str:
        lines = [f"# Discussion #{discussion_id} — History\n"]
        for msg in messages:
            prefix = msg.agent_role_name or msg.role
            lines.append(f"### [{prefix}]\n{msg.content}\n")

        fd, path = tempfile.mkstemp(
            prefix=f"discussion_{discussion_id}_", suffix=".md"
        )
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines))
        return path

    # ------------------------------------------------------------------
    # Facilitator: initial role assignment
    # ------------------------------------------------------------------
    async def _run_facilitator_init(
        self, disc: Discussion, history_file: str
    ) -> list[dict]:
        prompt = f"""\
You are a discussion facilitator. Your job is to analyze the conversation so far
and decide which expert perspectives should respond to the latest message.

Read the discussion history at: {history_file}

After reading, decide how many experts should respond (1 to {disc.max_agents}) and
what role each should take. Choose roles that ADD NEW PERSPECTIVES not already
well-covered in the discussion.

Respond with ONLY a JSON array, no other text:
[
  {{"role_name": "角色名 (e.g. 架构师, 安全顾问, 成本分析)", "system_prompt": "你是...的专家，从...角度分析问题", "brief": "一句话说明为什么需要这个角色"}},
  ...
]

Rules:
- 1 to {disc.max_agents} roles max
- Role names in Chinese
- system_prompt should be specific and actionable
- If the discussion just started, pick 2-3 foundational perspectives
- If a perspective is already well-covered, don't repeat it
- ONLY output the JSON array"""

        try:
            collected_text = await self._run_facilitator_process(disc, prompt)
        except Exception as e:
            raise RuntimeError(f"Facilitator failed: {e}")

        raw = "\n".join(collected_text)
        return self._parse_facilitator_roles(raw, disc.max_agents)

    async def _run_facilitator_add_one(
        self, disc: Discussion, history_file: str, existing_roles: list[str]
    ) -> dict:
        roles_str = ", ".join(existing_roles) if existing_roles else "(none)"
        prompt = f"""\
You are a discussion facilitator. The discussion already has these expert roles: {roles_str}

Read the discussion history at: {history_file}

Decide ONE new expert perspective that is currently MISSING and would add the most value.
Do NOT repeat any existing role.

Respond with ONLY a single JSON object, no other text:
{{"role_name": "角色名", "system_prompt": "你是...的专家，从...角度分析问题", "brief": "一句话说明为什么需要这个角色"}}

Rules:
- Role name in Chinese
- system_prompt should be specific and actionable
- Must be different from existing roles: {roles_str}
- ONLY output the JSON object"""

        try:
            collected_text = await self._run_facilitator_process(disc, prompt)
        except Exception as e:
            raise RuntimeError(f"Facilitator failed: {e}")

        raw = "\n".join(collected_text).strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "role_name" in data and "system_prompt" in data:
                return data
        except (json.JSONDecodeError, TypeError):
            pass

        logger.warning("Could not parse add-one response, using fallback. Raw: %s", raw[:200])
        return {
            "role_name": "补充视角",
            "system_prompt": "你是一位综合分析师，从其他角色尚未覆盖的角度提供分析和建议。",
            "brief": "补充缺失的分析视角",
        }

    def _parse_facilitator_roles(
        self, raw: str, max_agents: int
    ) -> list[dict]:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
            if isinstance(data, list):
                roles = []
                for item in data[:max_agents]:
                    if (
                        isinstance(item, dict)
                        and "role_name" in item
                        and "system_prompt" in item
                    ):
                        roles.append(item)
                if roles:
                    return roles
        except (json.JSONDecodeError, TypeError):
            pass

        logger.warning("Could not parse facilitator response, using defaults. Raw: %s", text[:200])
        return [
            {
                "role_name": "技术架构师",
                "system_prompt": "你是一位资深技术架构师，擅长系统设计、可扩展性分析和技术选型。从架构层面分析问题。",
                "brief": "提供架构层面的分析",
            },
            {
                "role_name": "产品视角",
                "system_prompt": "你是一位产品经理，擅长用户需求分析、优先级排序和 MVP 定义。从产品和用户体验角度分析问题。",
                "brief": "提供产品和用户视角",
            },
        ]


def _msg_to_dict(msg: DiscussionMessage) -> dict:
    return {
        "id": msg.id,
        "discussion_id": msg.discussion_id,
        "role": msg.role,
        "agent_role_name": msg.agent_role_name,
        "content": msg.content,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


def _agent_to_dict(agent: DiscussionAgent) -> dict:
    return {
        "id": agent.id,
        "discussion_id": agent.discussion_id,
        "role_name": agent.role_name,
        "system_prompt": agent.system_prompt,
        "session_id": agent.session_id,
        "status": agent.status,
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }
