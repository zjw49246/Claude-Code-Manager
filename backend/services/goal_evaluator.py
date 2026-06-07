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


class GoalEvaluator:
    """Evaluate a goal condition against a conversation transcript."""

    async def evaluate(
        self,
        condition: str,
        conversation_summary: str,
        model: str | None = None,
        provider: str = "claude",
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

        cmd = self._build_eval_command(provider, prompt, eval_model)

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
        except asyncio.TimeoutError:
            logger.warning("Goal evaluation timed out")
            return GoalEvalResult(achieved=False, reason="Evaluation timed out")
        except Exception as e:
            logger.error(f"Goal evaluation failed: {e}")
            return GoalEvalResult(achieved=False, reason=f"Evaluation error: {e}")

        raw = stdout.decode("utf-8", errors="replace")
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
