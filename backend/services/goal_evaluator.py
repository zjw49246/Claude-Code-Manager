"""Goal condition evaluator using a lightweight Claude model.

Spawns a short-lived `claude -p` subprocess (default Haiku) to judge whether
the conversation so far satisfies the user's goal condition.  The evaluator
only reads the conversation transcript — it cannot call tools or read files.
"""
import asyncio
import json
import logging
import os
import signal

from backend.config import settings
from backend.services.process_safety import require_safe_process_group_id

logger = logging.getLogger(__name__)

_PROCESS_CLEANUP_TIMEOUT = 5.0
# Exact handles remain reachable when cleanup cannot prove a process tree
# terminal.  Evaluators are otherwise short-lived local objects, so swallowing
# a reap failure would make the surviving child completely invisible.
_UNREAPED_GOAL_EVALUATOR_PROCESSES: dict[
    int, asyncio.subprocess.Process
] = {}
# Task ownership is kept separately to preserve the exact-handle registry's
# existing shape for shutdown/reap callers.  Entries exist from spawn until
# terminal proof, so task deletion can fail closed for both an active evaluator
# and one retained after cleanup failure.
_GOAL_EVALUATOR_TASK_IDS: dict[int, int | None] = {}


class GoalEvalResult:
    __slots__ = ("achieved", "reason")

    def __init__(self, achieved: bool, reason: str):
        self.achieved = achieved
        self.reason = reason


class GoalEvaluationError(RuntimeError):
    """Operational evaluator failure with output preserved for classification."""

    __slots__ = ("provider", "returncode", "stdout", "stderr")

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ):
        detail = stderr.strip() or stdout.strip()
        suffix = f": {detail[:1000]}" if detail else ""
        super().__init__(f"{message}{suffix}")
        self.provider = provider
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def combined_output(self) -> str:
        """Text consumed by the provider's usage/auth failure classifier."""
        parts = [part.strip() for part in (self.stderr, self.stdout) if part.strip()]
        return "\n".join(parts) or str(self)


class GoalEvaluatorCleanupError(RuntimeError):
    """A goal-evaluator process tree could not be proven terminal."""


def _managed_process_group_pid(
    process: asyncio.subprocess.Process | None,
    managed_process_group: bool,
) -> int | None:
    """Return the exact POSIX process-group id created for this evaluator."""

    if not managed_process_group or process is None:
        return None
    return require_safe_process_group_id(
        getattr(process, "pid", None),
        context="goal evaluator",
    )


def _process_group_alive(process_group_id: int | None) -> bool:
    """Conservatively report whether an exact POSIX process group remains."""

    if process_group_id is None:
        return False
    process_group_id = require_safe_process_group_id(
        process_group_id,
        context="goal evaluator liveness check",
    )
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _settle_process_spawn(
    *cmd: str,
    **spawn_kwargs,
) -> tuple[asyncio.subprocess.Process, asyncio.CancelledError | None]:
    """Return the exact spawned process even across caller cancellation."""

    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_exec(*cmd, **spawn_kwargs)
    )
    delayed_cancellation: asyncio.CancelledError | None = None
    while not spawn_task.done():
        try:
            await asyncio.shield(spawn_task)
        except asyncio.CancelledError as exc:
            if spawn_task.done():
                break
            delayed_cancellation = exc
        except Exception:
            break

    try:
        process = spawn_task.result()
    except BaseException:
        if delayed_cancellation is not None:
            raise delayed_cancellation
        raise
    return process, delayed_cancellation


async def _terminate_process(
    process: asyncio.subprocess.Process | None,
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None,
    *,
    managed_process_group: bool,
) -> None:
    """Kill and reap one evaluator process tree without leaving pipe readers."""

    if process is None:
        if communicate_task is not None and not communicate_task.done():
            communicate_task.cancel()
            await asyncio.gather(communicate_task, return_exceptions=True)
        return

    process_group_id = _managed_process_group_pid(
        process, managed_process_group
    )
    try:
        if process_group_id is not None:
            os.killpg(process_group_id, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        # A just-exited group may race this signal.  If the parent itself is
        # still reported alive, retain the portable single-process fallback.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    except Exception:
        logger.exception("Failed to stop goal evaluator process")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PROCESS_CLEANUP_TIMEOUT
    parent_reaped = process.returncode is not None

    # evaluate() keeps communicate() alive behind a shield.  Awaiting that
    # exact task drains both PIPEs while it also waits for the killed parent.
    # This matters when a child inherited either descriptor.
    if communicate_task is not None:
        try:
            await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=max(0.01, deadline - loop.time()),
            )
            # asyncio.subprocess.Process.communicate() includes wait().
            parent_reaped = True
        except asyncio.TimeoutError:
            logger.error("Timed out draining goal evaluator output")
            communicate_task.cancel()
            await asyncio.gather(communicate_task, return_exceptions=True)
        except Exception:
            # The original communicate failure is reported by evaluate(); the
            # remaining responsibility here is to reap the process.
            pass

    try:
        if process.returncode is None:
            await asyncio.wait_for(
                asyncio.shield(process.wait()),
                timeout=max(0.01, deadline - loop.time()),
            )
            parent_reaped = True
    except asyncio.TimeoutError:
        logger.error("Timed out reaping goal evaluator process")
    except Exception:
        logger.exception("Failed to reap goal evaluator process")

    while _process_group_alive(process_group_id):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RuntimeError(
                f"Goal evaluator process group {process_group_id} survived SIGKILL"
            )
        await asyncio.sleep(min(0.05, remaining))
    if not parent_reaped:
        raise RuntimeError(
            f"Goal evaluator process {getattr(process, 'pid', None)} "
            "could not be reaped"
        )


async def _terminate_process_shielded(
    process: asyncio.subprocess.Process | None,
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None,
    *,
    managed_process_group: bool,
    delayed_cancellation: asyncio.CancelledError | None = None,
) -> None:
    """Finish process cleanup before delivering caller cancellation."""

    cleanup = asyncio.create_task(
        _terminate_process(
            process,
            communicate_task,
            managed_process_group=managed_process_group,
        )
    )
    cancellation = delayed_cancellation
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            # Multiple cancellations must not strand the evaluator.  Preserve
            # the latest one and keep waiting for the shielded cleanup task.
            cancellation = exc
        except Exception:
            # Inspect and classify the settled cleanup failure below, where the
            # exact process handle is retained before propagating it.
            break

    try:
        cleanup.result()
    except Exception as exc:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0 and process is not None:
            _UNREAPED_GOAL_EVALUATOR_PROCESSES[pid] = process
        logger.exception("Goal evaluator cleanup failed")
        raise GoalEvaluatorCleanupError(
            f"Goal evaluator process group {pid} could not be proven terminal"
        ) from exc
    else:
        pid = getattr(process, "pid", None)
        if (
            isinstance(pid, int)
            and _UNREAPED_GOAL_EVALUATOR_PROCESSES.get(pid) is process
        ):
            _UNREAPED_GOAL_EVALUATOR_PROCESSES.pop(pid, None)
            _GOAL_EVALUATOR_TASK_IDS.pop(pid, None)

    if cancellation is not None:
        raise cancellation


async def reap_unreaped_goal_evaluators() -> None:
    """Retry every exact evaluator process retained after cleanup failure."""

    failures: list[str] = []
    for pid, process in list(_UNREAPED_GOAL_EVALUATOR_PROCESSES.items()):
        try:
            await _terminate_process_shielded(
                process,
                None,
                managed_process_group=(os.name == "posix"),
            )
        except Exception as exc:
            failures.append(f"pid {pid}: {exc}")
    if failures:
        raise GoalEvaluatorCleanupError(
            "Could not reap retained goal evaluator processes: "
            + "; ".join(failures)
        )


def has_unreaped_goal_evaluator_for_task(task_id: int) -> bool:
    """Whether an active/retained evaluator still owns this Task."""

    return any(
        pid in _UNREAPED_GOAL_EVALUATOR_PROCESSES
        and owner_task_id == task_id
        for pid, owner_task_id in _GOAL_EVALUATOR_TASK_IDS.items()
    )


class GoalEvaluator:
    """Evaluate a goal condition against a conversation transcript."""

    async def evaluate(
        self,
        condition: str,
        conversation_summary: str,
        model: str | None = None,
        provider: str = "claude",
        codex_home: str | None = None,
        task_id: int | None = None,
    ) -> GoalEvalResult:
        provider = (provider or "claude").lower()
        if provider == "codex":
            eval_model = model or settings.default_codex_goal_evaluator_model
        else:
            eval_model = model or settings.default_goal_evaluator_model

        prompt = self._build_eval_prompt(condition, conversation_summary)

        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")
        }
        if provider == "codex" and codex_home:
            env["CODEX_HOME"] = os.path.expandvars(os.path.expanduser(codex_home))

        cmd = self._build_eval_command(provider, prompt, eval_model)

        process: asyncio.subprocess.Process | None = None
        communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        managed_process_group = os.name == "posix"
        spawn_kwargs: dict[str, object] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": env,
        }
        if managed_process_group:
            spawn_kwargs["start_new_session"] = True
        try:
            process, spawn_cancellation = await _settle_process_spawn(
                *cmd,
                **spawn_kwargs,
            )
            pid = getattr(process, "pid", None)
            if isinstance(pid, int) and pid > 0:
                _UNREAPED_GOAL_EVALUATOR_PROCESSES[pid] = process
                _GOAL_EVALUATOR_TASK_IDS[pid] = task_id
            if spawn_cancellation is not None:
                await _terminate_process_shielded(
                    process,
                    communicate_task,
                    managed_process_group=managed_process_group,
                    delayed_cancellation=spawn_cancellation,
                )
            communicate_task = asyncio.create_task(process.communicate())
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=settings.goal_evaluation_timeout,
            )
            # communicate() proving the CLI parent exited does not prove its
            # dedicated group is empty: a detached tool child can close stdio
            # and continue running.  Always sweep and verify that exact group.
            await _terminate_process_shielded(
                process,
                communicate_task,
                managed_process_group=managed_process_group,
            )
        except asyncio.CancelledError as exc:
            logger.info("Goal evaluation cancelled")
            await _terminate_process_shielded(
                process,
                communicate_task,
                managed_process_group=managed_process_group,
                delayed_cancellation=exc,
            )
            raise
        except asyncio.TimeoutError as exc:
            logger.warning("Goal evaluation timed out")
            await _terminate_process_shielded(
                process,
                communicate_task,
                managed_process_group=managed_process_group,
            )
            returncode = (
                process.returncode
                if process is not None and isinstance(process.returncode, int)
                else None
            )
            raise GoalEvaluationError(
                "Goal evaluation timed out",
                provider=provider,
                returncode=returncode,
            ) from exc
        except Exception as exc:
            logger.error("Goal evaluation failed: %s", exc)
            await _terminate_process_shielded(
                process,
                communicate_task,
                managed_process_group=managed_process_group,
            )
            returncode = (
                process.returncode
                if process is not None and isinstance(process.returncode, int)
                else None
            )
            raise GoalEvaluationError(
                "Goal evaluation process failed",
                provider=provider,
                returncode=returncode,
                stderr=str(exc),
            ) from exc

        raw = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        returncode = process.returncode if isinstance(process.returncode, int) else 0
        if returncode != 0 or (not raw.strip() and stderr_text.strip()):
            logger.warning(
                "Goal evaluation exited with code %s: %s",
                returncode,
                stderr_text.strip()[:500],
            )
            raise GoalEvaluationError(
                f"Goal evaluation exited with code {returncode}",
                provider=provider,
                returncode=returncode,
                stdout=raw,
                stderr=stderr_text,
            )
        if provider == "codex":
            return self._parse_codex_response(raw)
        return self._parse_response(raw)

    def _build_eval_command(self, provider: str, prompt: str, model: str) -> list[str]:
        if provider == "codex":
            return [
                settings.codex_binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--ephemeral",
                "--model", model,
                prompt,
            ]
        return [
            settings.claude_binary,
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "json",
            "--model", model,
            "--max-turns", "1",
        ]

    def _build_eval_prompt(self, condition: str, conversation_summary: str) -> str:
        return f"""\
You are a goal evaluator. Your ONLY job is to judge whether a goal condition
has been met based on the conversation transcript below.

## Goal Condition
{condition}

## Conversation Transcript (most recent work)
{conversation_summary}

## Instructions
Based on the transcript, determine if the goal condition has been fully achieved.
You must respond with EXACTLY one JSON object (no other text):

If achieved:
{{"achieved": true, "reason": "brief explanation of why the condition is met"}}

If NOT achieved:
{{"achieved": false, "reason": "brief explanation of what still needs to be done"}}

Respond with ONLY the JSON object, nothing else."""

    def _parse_codex_response(self, raw: str) -> GoalEvalResult:
        """Parse Codex JSONL output — extract the last agent_message text."""
        text = ""
        for line in raw.strip().splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = data.get("item") if isinstance(data.get("item"), dict) else {}
            if item.get("type") == "agent_message" and item.get("text"):
                text = item["text"]
        if not text:
            text = raw
        return self._extract_eval_json(text)

    def _parse_response(self, raw: str) -> GoalEvalResult:
        text = raw.strip()

        # claude --output-format json wraps the response in a JSON envelope
        try:
            envelope = json.loads(text)
            if isinstance(envelope, dict) and "result" in envelope:
                text = envelope["result"]
            elif isinstance(envelope, dict) and "content" in envelope:
                text = envelope["content"]
        except (json.JSONDecodeError, TypeError):
            pass

        return self._extract_eval_json(text)

    def _extract_eval_json(self, text) -> GoalEvalResult:
        """Extract {achieved, reason} JSON from text (may be wrapped in markdown)."""
        if isinstance(text, str):
            cleaned = text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines).strip()

            try:
                data = json.loads(cleaned)
                if isinstance(data, dict) and "achieved" in data:
                    return GoalEvalResult(
                        achieved=bool(data["achieved"]),
                        reason=data.get("reason", ""),
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        logger.warning(f"Could not parse evaluator response: {str(text)[:200]}")
        return GoalEvalResult(
            achieved=False,
            reason="Could not parse evaluator response",
        )
