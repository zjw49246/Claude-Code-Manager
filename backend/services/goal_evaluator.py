"""Goal condition evaluator using a lightweight Claude model.

Spawns a short-lived `claude -p` subprocess (default Haiku) to judge whether
the conversation so far satisfies the user's goal condition.  The evaluator
only reads the conversation transcript — it cannot call tools or read files.
"""
import asyncio
import json
import logging
import os

from backend.config import settings

logger = logging.getLogger(__name__)


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


async def _terminate_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except Exception:
        logger.exception("Failed to stop goal evaluator process")
    try:
        await process.wait()
    except Exception:
        logger.exception("Failed to reap goal evaluator process")


class GoalEvaluator:
    """Evaluate a goal condition against a conversation transcript."""

    async def evaluate(
        self,
        condition: str,
        conversation_summary: str,
        model: str | None = None,
        provider: str = "claude",
        codex_home: str | None = None,
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
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.goal_evaluation_timeout,
            )
        except asyncio.TimeoutError as exc:
            logger.warning("Goal evaluation timed out")
            await _terminate_process(process)
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
            await _terminate_process(process)
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
