"""Regression tests for fail-closed process-group signalling.

``os.killpg(1, signal)`` is especially dangerous on POSIX because it is
implemented as ``kill(-1, signal)``.  Keep these tests entirely mocked so a
regression can never emit a real signal while it is being detected.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.dispatcher import GlobalDispatcher
from backend.services.goal_evaluator import _terminate_process
from backend.services.process_safety import UnsafeProcessGroupError


def _mock_process(pid: object) -> MagicMock:
    process = MagicMock()
    process.pid = pid
    process.returncode = None
    process.kill = MagicMock()
    process.send_signal = MagicMock()
    process.wait = AsyncMock(return_value=-9)
    return process


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
@pytest.mark.parametrize(
    "unsafe_pid",
    [None, 1, 0, -1, True, False],
    ids=[
        "missing",
        "pid-1",
        "pid-0",
        "pid-minus-1",
        "bool-true",
        "bool-false",
    ],
)
async def test_goal_evaluator_rejects_unsafe_group_without_signalling(
    unsafe_pid: object,
):
    process = _mock_process(unsafe_pid)

    with patch("backend.services.goal_evaluator.os.killpg") as killpg:
        with pytest.raises(UnsafeProcessGroupError):
            await _terminate_process(
                process,
                None,
                managed_process_group=True,
            )

    killpg.assert_not_called()
    process.kill.assert_not_called()
    process.send_signal.assert_not_called()
    process.wait.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
@pytest.mark.parametrize(
    "unsafe_pid",
    [None, 1, 0, -1, True, False],
    ids=[
        "missing",
        "pid-1",
        "pid-0",
        "pid-minus-1",
        "bool-true",
        "bool-false",
    ],
)
async def test_dispatcher_rejects_unsafe_aux_group_without_signalling(
    unsafe_pid: object,
):
    process = _mock_process(unsafe_pid)

    with patch("backend.services.dispatcher.os.killpg") as killpg:
        with pytest.raises(UnsafeProcessGroupError):
            await GlobalDispatcher._terminate_aux_process(process, timeout=0.01)

    killpg.assert_not_called()
    process.kill.assert_not_called()
    process.send_signal.assert_not_called()
    process.wait.assert_not_awaited()
