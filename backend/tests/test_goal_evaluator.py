"""Tests for GoalEvaluator — parsing and evaluation logic."""
import asyncio
import json
import os
import signal
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.goal_evaluator import (
    GoalEvaluationError,
    GoalEvaluatorCleanupError,
    GoalEvaluator,
    GoalEvalResult,
    _GOAL_EVALUATOR_TASK_IDS,
    _UNREAPED_GOAL_EVALUATOR_PROCESSES,
    has_unreaped_goal_evaluator_for_task,
    reap_unreaped_goal_evaluators,
)


def _pid_is_running(pid: int) -> bool:
    """Treat a reparented zombie as stopped for process-leak assertions."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    stat_path = f"/proc/{pid}/stat"
    try:
        with open(stat_path, encoding="utf-8") as stat_file:
            stat = stat_file.read()
    except (FileNotFoundError, PermissionError):
        return True
    close_paren = stat.rfind(")")
    state = stat[close_paren + 2:].split()[0] if close_paren >= 0 else ""
    return state != "Z"


async def _wait_for_child_pid(path, task: asyncio.Task, timeout: float = 2.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if path.exists():
            return int(path.read_text(encoding="utf-8"))
        if task.done():
            await task
            raise AssertionError("Evaluator exited before spawning its child")
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for evaluator child PID")


async def _wait_until_not_running(pid: int, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not _pid_is_running(pid):
            return
        await asyncio.sleep(0.02)
    assert not _pid_is_running(pid), f"Process {pid} is still running"


async def _force_cleanup_process_tree(process, child_pid: int | None) -> None:
    """Explicit, validated test cleanup for failed real-process assertions."""

    parent_pid = getattr(process, "pid", None) if process is not None else None
    if os.name == "posix" and isinstance(parent_pid, int) and parent_pid > 1:
        try:
            os.killpg(parent_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if isinstance(child_pid, int) and child_pid > 1 and _pid_is_running(child_pid):
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process is not None and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


def _process_tree_command(pid_file) -> list[str]:
    script = """
import pathlib
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
print("child-started", flush=True)
time.sleep(30)
"""
    return [sys.executable, "-c", script, str(pid_file)]


def _successful_process_tree_command(pid_file) -> list[str]:
    """Exit parent cleanly while a same-group child with closed stdio remains."""

    script = """
import json
import pathlib
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
print(json.dumps({"achieved": True, "reason": "done"}), flush=True)
"""
    return [sys.executable, "-c", script, str(pid_file)]


class TestGoalEvalResult:
    def test_slots(self):
        r = GoalEvalResult(achieved=True, reason="done")
        assert r.achieved is True
        assert r.reason == "done"

    def test_false_result(self):
        r = GoalEvalResult(achieved=False, reason="not yet")
        assert r.achieved is False
        assert r.reason == "not yet"


@pytest.mark.asyncio
async def test_retained_goal_evaluator_is_retried_by_shutdown_reaper():
    process = MagicMock(pid=54_901, returncode=None)
    _UNREAPED_GOAL_EVALUATOR_PROCESSES[process.pid] = process
    try:
        with patch(
            "backend.services.goal_evaluator._terminate_process",
            new_callable=AsyncMock,
            side_effect=RuntimeError("still alive"),
        ):
            with pytest.raises(GoalEvaluatorCleanupError, match="54901"):
                await reap_unreaped_goal_evaluators()
        assert _UNREAPED_GOAL_EVALUATOR_PROCESSES[process.pid] is process

        with patch(
            "backend.services.goal_evaluator._terminate_process",
            new_callable=AsyncMock,
        ):
            await reap_unreaped_goal_evaluators()
        assert process.pid not in _UNREAPED_GOAL_EVALUATOR_PROCESSES
    finally:
        _UNREAPED_GOAL_EVALUATOR_PROCESSES.pop(process.pid, None)
        _GOAL_EVALUATOR_TASK_IDS.pop(process.pid, None)


def test_retained_goal_evaluator_is_queryable_by_task():
    process = MagicMock(pid=54_902, returncode=None)
    _UNREAPED_GOAL_EVALUATOR_PROCESSES[process.pid] = process
    _GOAL_EVALUATOR_TASK_IDS[process.pid] = 812
    try:
        assert has_unreaped_goal_evaluator_for_task(812) is True
        assert has_unreaped_goal_evaluator_for_task(813) is False
    finally:
        _UNREAPED_GOAL_EVALUATOR_PROCESSES.pop(process.pid, None)
        _GOAL_EVALUATOR_TASK_IDS.pop(process.pid, None)


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
        mock_proc.pid = 55_001
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=ProcessLookupError,
            ),
        ):
            result = await evaluator.evaluate(
                condition="all tests pass",
                conversation_summary="pytest: 10 passed, 0 failed",
            )

        assert result.achieved is True
        assert result.reason == "all tests pass"

    @pytest.mark.asyncio
    async def test_posix_evaluator_starts_in_dedicated_session(self):
        evaluator = GoalEvaluator()
        mock_result = json.dumps({"achieved": True, "reason": "ok"})
        mock_proc = MagicMock()
        mock_proc.pid = 55_002
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=ProcessLookupError,
            ),
        ):
            await evaluator.evaluate(condition="cond", conversation_summary="conv")

        if os.name == "posix":
            assert mock_exec.call_args.kwargs["start_new_session"] is True

    @pytest.mark.asyncio
    async def test_non_posix_evaluator_uses_portable_spawn_and_kill(self):
        evaluator = GoalEvaluator()
        mock_proc = MagicMock()
        mock_proc.pid = None
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=-9)

        with (
            patch("backend.services.goal_evaluator.os.name", "nt"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        ):
            with pytest.raises(GoalEvaluationError):
                await evaluator.evaluate(condition="cond", conversation_summary="conv")

        assert "start_new_session" not in mock_exec.call_args.kwargs
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_evaluate_not_achieved(self):
        evaluator = GoalEvaluator()
        mock_result = json.dumps({"achieved": False, "reason": "3 tests still failing"})

        mock_proc = MagicMock()
        mock_proc.pid = 55_003
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=ProcessLookupError,
            ),
        ):
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
        mock_proc.pid = 55_004
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=-9)

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=ProcessLookupError,
            ),
        ):
            with pytest.raises(GoalEvaluationError) as exc_info:
                await evaluator.evaluate(
                    condition="cond",
                    conversation_summary="conv",
                )

        assert "timed out" in str(exc_info.value).lower()
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_kills_exact_posix_process_group(self):
        if os.name != "posix":
            pytest.skip("POSIX process groups only")

        evaluator = GoalEvaluator()
        mock_proc = MagicMock()
        mock_proc.pid = 43210
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=-9)

        group_alive = True

        def kill_group(pid, sig):
            nonlocal group_alive
            assert pid == 43210
            if sig == 0:
                if group_alive:
                    return None
                raise ProcessLookupError
            assert sig == signal.SIGKILL
            group_alive = False

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=kill_group,
            ) as killpg,
        ):
            with pytest.raises(GoalEvaluationError):
                await evaluator.evaluate(condition="cond", conversation_summary="conv")

        assert (43210, signal.SIGKILL) in [
            call.args for call in killpg.call_args_list
        ]
        mock_proc.kill.assert_not_called()
        mock_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_repeated_cancellation_waits_for_shielded_reap(self):
        evaluator = GoalEvaluator()
        communicate_started = asyncio.Event()
        communicate_released = asyncio.Event()
        wait_started = asyncio.Event()
        wait_released = asyncio.Event()

        async def communicate():
            communicate_started.set()
            await communicate_released.wait()
            return b"", b""

        async def wait():
            wait_started.set()
            await wait_released.wait()
            return -9

        mock_proc = MagicMock()
        mock_proc.pid = 55_009
        mock_proc.returncode = None
        mock_proc.communicate = AsyncMock(side_effect=communicate)
        mock_proc.wait = AsyncMock(side_effect=wait)
        mock_proc.kill = MagicMock()

        group_alive = True

        def kill_group(pid, sig):
            nonlocal group_alive
            assert pid == 55_009
            if sig == 0:
                if group_alive:
                    return None
                raise ProcessLookupError
            assert sig == signal.SIGKILL
            group_alive = False
            communicate_released.set()

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=kill_group,
            ) as killpg,
        ):
            evaluation = asyncio.create_task(
                evaluator.evaluate(condition="cond", conversation_summary="conv")
            )
            await communicate_started.wait()
            evaluation.cancel()
            await wait_started.wait()
            evaluation.cancel()
            assert not evaluation.done()
            wait_released.set()
            with pytest.raises(asyncio.CancelledError):
                await evaluation

        assert (55_009, signal.SIGKILL) in [
            call.args for call in killpg.call_args_list
        ]
        mock_proc.kill.assert_not_called()
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
        mock_proc.pid = 55_005
        mock_proc.communicate = AsyncMock(return_value=(
            b'{"type":"turn.failed"}\n',
            b"You have hit your usage limit. Try again later.",
        ))
        mock_proc.returncode = 1

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=ProcessLookupError,
            ),
        ):
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
        mock_proc.pid = 55_006
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=ProcessLookupError,
            ),
        ):
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
        mock_proc.pid = 55_007
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=ProcessLookupError,
            ),
        ):
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
        mock_proc.pid = 55_008
        mock_proc.communicate = AsyncMock(return_value=(stdout, b""))
        mock_proc.returncode = 0
        codex_home = tmp_path / "codex-account-2"

        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
            patch(
                "backend.services.goal_evaluator.os.killpg",
                side_effect=ProcessLookupError,
            ),
        ):
            result = await evaluator.evaluate(
                condition="cond",
                conversation_summary="conv",
                provider="codex",
                codex_home=str(codex_home),
            )

        assert result.achieved is True
        assert mock_exec.call_args.kwargs["env"]["CODEX_HOME"] == str(codex_home)
        assert mock_exec.call_args.args[1] == "exec"

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
    async def test_normal_completion_kills_closed_stdio_descendant(self, tmp_path):
        evaluator = GoalEvaluator()
        pid_file = tmp_path / "normal-child.pid"
        captured: dict[str, object] = {}
        child_pid: int | None = None
        real_create_subprocess_exec = asyncio.create_subprocess_exec

        async def capture_process(*args, **kwargs):
            process = await real_create_subprocess_exec(*args, **kwargs)
            captured["process"] = process
            return process

        try:
            with (
                patch.object(
                    evaluator,
                    "_build_eval_command",
                    return_value=_successful_process_tree_command(pid_file),
                ),
                patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=capture_process,
                ),
            ):
                result = await evaluator.evaluate(
                    condition="cond",
                    conversation_summary="conv",
                )
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            assert result.achieved is True
            await _wait_until_not_running(child_pid)
            assert captured["process"].returncode == 0
        finally:
            await _force_cleanup_process_tree(captured.get("process"), child_pid)

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
    async def test_spawn_cancellation_settles_and_reaps_process_tree(
        self, tmp_path
    ):
        evaluator = GoalEvaluator()
        pid_file = tmp_path / "spawn-cancel-child.pid"
        captured: dict[str, object] = {}
        spawned = asyncio.Event()
        release_spawn = asyncio.Event()
        child_pid: int | None = None
        evaluation: asyncio.Task | None = None
        real_create_subprocess_exec = asyncio.create_subprocess_exec

        async def delayed_spawn(*args, **kwargs):
            process = await real_create_subprocess_exec(*args, **kwargs)
            captured["process"] = process
            spawned.set()
            await release_spawn.wait()
            return process

        try:
            with (
                patch.object(
                    evaluator,
                    "_build_eval_command",
                    return_value=_process_tree_command(pid_file),
                ),
                patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=delayed_spawn,
                ),
            ):
                evaluation = asyncio.create_task(
                    evaluator.evaluate(
                        condition="cond",
                        conversation_summary="conv",
                    )
                )
                await spawned.wait()
                child_pid = await _wait_for_child_pid(pid_file, evaluation)
                evaluation.cancel()
                await asyncio.sleep(0)
                assert not evaluation.done()
                release_spawn.set()
                with pytest.raises(asyncio.CancelledError):
                    await evaluation

            process = captured["process"]
            assert process.returncode is not None
            await _wait_until_not_running(child_pid)
        finally:
            release_spawn.set()
            if evaluation is not None and not evaluation.done():
                evaluation.cancel()
            await _force_cleanup_process_tree(captured.get("process"), child_pid)

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
    async def test_real_timeout_kills_child_that_inherits_output_pipes(self, tmp_path):
        evaluator = GoalEvaluator()
        pid_file = tmp_path / "timeout-child.pid"
        captured: dict[str, object] = {}
        child_pid: int | None = None
        real_create_subprocess_exec = asyncio.create_subprocess_exec

        async def capture_process(*args, **kwargs):
            process = await real_create_subprocess_exec(*args, **kwargs)
            captured["process"] = process
            return process

        try:
            with (
                patch.object(
                    evaluator,
                    "_build_eval_command",
                    return_value=_process_tree_command(pid_file),
                ),
                patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=capture_process,
                ),
                patch(
                    "backend.services.goal_evaluator.settings.goal_evaluation_timeout",
                    0.3,
                ),
            ):
                with pytest.raises(GoalEvaluationError, match="timed out"):
                    await evaluator.evaluate(
                        condition="cond",
                        conversation_summary="conv",
                    )

            process = captured["process"]
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            assert process.returncode is not None
            await _wait_until_not_running(child_pid)
        finally:
            await _force_cleanup_process_tree(captured.get("process"), child_pid)

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
    async def test_real_cancellation_reaps_parent_and_inherited_pipe_child(
        self, tmp_path
    ):
        evaluator = GoalEvaluator()
        pid_file = tmp_path / "cancel-child.pid"
        captured: dict[str, object] = {}
        child_pid: int | None = None
        real_create_subprocess_exec = asyncio.create_subprocess_exec

        async def capture_process(*args, **kwargs):
            process = await real_create_subprocess_exec(*args, **kwargs)
            captured["process"] = process
            return process

        evaluation: asyncio.Task | None = None
        try:
            with (
                patch.object(
                    evaluator,
                    "_build_eval_command",
                    return_value=_process_tree_command(pid_file),
                ),
                patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=capture_process,
                ),
            ):
                evaluation = asyncio.create_task(
                    evaluator.evaluate(
                        condition="cond",
                        conversation_summary="conv",
                    )
                )
                child_pid = await _wait_for_child_pid(pid_file, evaluation)
                evaluation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await evaluation

            process = captured["process"]
            assert process.returncode is not None
            await _wait_until_not_running(child_pid)
        finally:
            if evaluation is not None and not evaluation.done():
                evaluation.cancel()
            await _force_cleanup_process_tree(captured.get("process"), child_pid)
