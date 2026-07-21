"""Tests for GoalEvaluator — parsing and evaluation logic."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.goal_evaluator import (
    GoalEvaluationError,
    GoalEvaluator,
    GoalEvalResult,
)


class TestGoalEvalResult:
    def test_slots(self):
        r = GoalEvalResult(achieved=True, reason="done")
        assert r.achieved is True
        assert r.reason == "done"

    def test_false_result(self):
        r = GoalEvalResult(achieved=False, reason="not yet")
        assert r.achieved is False
        assert r.reason == "not yet"


class TestParseResponse:
    def setup_method(self):
        self.evaluator = GoalEvaluator()

    def test_direct_json(self):
        raw = json.dumps({"achieved": True, "reason": "all tests pass"})
        result = self.evaluator._parse_response(raw)
        assert result.achieved is True
        assert result.reason == "all tests pass"

    def test_json_in_result_envelope(self):
        envelope = {"result": json.dumps({"achieved": False, "reason": "2 tests fail"})}
        result = self.evaluator._parse_response(json.dumps(envelope))
        assert result.achieved is False
        assert result.reason == "2 tests fail"

    def test_json_in_content_envelope(self):
        envelope = {"content": json.dumps({"achieved": True, "reason": "done"})}
        result = self.evaluator._parse_response(json.dumps(envelope))
        assert result.achieved is True

    def test_json_in_markdown_code_block(self):
        raw = '```json\n{"achieved": true, "reason": "clean"}\n```'
        result = self.evaluator._parse_response(raw)
        assert result.achieved is True
        assert result.reason == "clean"

    def test_malformed_response(self):
        result = self.evaluator._parse_response("I think the goal is met")
        assert result.achieved is False
        assert "Could not parse" in result.reason

    def test_empty_response(self):
        result = self.evaluator._parse_response("")
        assert result.achieved is False

    def test_achieved_false_string(self):
        raw = json.dumps({"achieved": False, "reason": "lint errors remain"})
        result = self.evaluator._parse_response(raw)
        assert result.achieved is False
        assert result.reason == "lint errors remain"

    def test_missing_reason_field(self):
        raw = json.dumps({"achieved": True})
        result = self.evaluator._parse_response(raw)
        assert result.achieved is True
        assert result.reason == ""

    def test_nested_result_with_direct_json(self):
        """Result envelope where result is already a dict (not a string)."""
        envelope = {"result": '{"achieved": true, "reason": "ok"}'}
        result = self.evaluator._parse_response(json.dumps(envelope))
        assert result.achieved is True


class TestBuildEvalPrompt:
    def setup_method(self):
        self.evaluator = GoalEvaluator()

    def test_contains_condition(self):
        prompt = self.evaluator._build_eval_prompt("all tests pass", "some conversation")
        assert "all tests pass" in prompt

    def test_contains_conversation(self):
        prompt = self.evaluator._build_eval_prompt("condition", "Claude ran pytest and got 0 failures")
        assert "Claude ran pytest and got 0 failures" in prompt

    def test_json_template_present(self):
        prompt = self.evaluator._build_eval_prompt("cond", "conv")
        assert '"achieved": true' in prompt
        assert '"achieved": false' in prompt


class TestEvaluateIntegration:
    """Test the evaluate method with mocked subprocess."""

    @pytest.mark.asyncio
    async def test_evaluate_achieved(self):
        evaluator = GoalEvaluator()
        mock_result = json.dumps({"achieved": True, "reason": "all tests pass"})

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await evaluator.evaluate(
                condition="all tests pass",
                conversation_summary="pytest: 10 passed, 0 failed",
            )

        assert result.achieved is True
        assert result.reason == "all tests pass"

    @pytest.mark.asyncio
    async def test_evaluate_not_achieved(self):
        evaluator = GoalEvaluator()
        mock_result = json.dumps({"achieved": False, "reason": "3 tests still failing"})

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await evaluator.evaluate(
                condition="all tests pass",
                conversation_summary="pytest: 7 passed, 3 failed",
            )

        assert result.achieved is False
        assert "3 tests still failing" in result.reason

    @pytest.mark.asyncio
    async def test_evaluate_timeout(self):
        evaluator = GoalEvaluator()

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=-9)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(GoalEvaluationError) as exc_info:
                await evaluator.evaluate(
                    condition="cond",
                    conversation_summary="conv",
                )

        assert "timed out" in str(exc_info.value).lower()
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_evaluate_subprocess_error(self):
        evaluator = GoalEvaluator()

        with patch("asyncio.create_subprocess_exec", side_effect=OSError("binary not found")):
            with pytest.raises(GoalEvaluationError) as exc_info:
                await evaluator.evaluate(
                    condition="cond",
                    conversation_summary="conv",
                )

        assert "binary not found" in exc_info.value.stderr
        assert "binary not found" in exc_info.value.combined_output

    @pytest.mark.asyncio
    async def test_nonzero_exit_exposes_stderr_for_pool_classification(self):
        evaluator = GoalEvaluator()
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(
            b'{"type":"turn.failed"}\n',
            b"You have hit your usage limit. Try again later.",
        ))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(GoalEvaluationError) as exc_info:
                await evaluator.evaluate(
                    condition="cond",
                    conversation_summary="conv",
                    provider="codex",
                    codex_home="/tmp/codex-a",
                )

        error = exc_info.value
        assert error.provider == "codex"
        assert error.returncode == 1
        assert "usage limit" in error.stderr
        assert "usage limit" in error.combined_output
        assert "turn.failed" in error.combined_output

    @pytest.mark.asyncio
    async def test_evaluate_uses_custom_model(self):
        evaluator = GoalEvaluator()
        mock_result = json.dumps({"achieved": True, "reason": "ok"})

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await evaluator.evaluate(
                condition="cond",
                conversation_summary="conv",
                model="claude-sonnet-4-6",
            )

        call_args = mock_exec.call_args[0]
        assert "--model" in call_args
        model_idx = list(call_args).index("--model")
        assert call_args[model_idx + 1] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_evaluate_passes_max_turns_1(self):
        evaluator = GoalEvaluator()
        mock_result = json.dumps({"achieved": True, "reason": "ok"})

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await evaluator.evaluate(
                condition="cond",
                conversation_summary="conv",
            )

        call_args = mock_exec.call_args[0]
        assert "--max-turns" in call_args
        idx = list(call_args).index("--max-turns")
        assert call_args[idx + 1] == "1"

    @pytest.mark.asyncio
    async def test_codex_evaluation_sets_explicit_codex_home(self, tmp_path):
        evaluator = GoalEvaluator()
        agent_text = json.dumps({"achieved": True, "reason": "ok"})
        stdout = json.dumps(
            {"item": {"type": "agent_message", "text": agent_text}}
        ).encode()
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(stdout, b""))
        codex_home = tmp_path / "codex-account-2"

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await evaluator.evaluate(
                condition="cond",
                conversation_summary="conv",
                provider="codex",
                codex_home=str(codex_home),
            )

        assert result.achieved is True
        assert mock_exec.call_args.kwargs["env"]["CODEX_HOME"] == str(codex_home)
        assert mock_exec.call_args.args[1] == "exec"
