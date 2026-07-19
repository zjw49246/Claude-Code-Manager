"""Real Codex latency A/B for ``codex exec`` versus CCM app-server.

This is intentionally a manual benchmark because every sample calls the real
Codex service and consumes quota. Run it from any directory with, for example:

    uv run python scripts/benchmark_codex_transport.py --samples 5

The two paths use the same cwd, model, effort, and prompt shape. Samples are
interleaved to reduce bias from short-lived service or network changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.codex_app_server import CodexAppServer  # noqa: E402


async def _direct_sample(
    *, index: int, binary: str, cwd: Path, model: str, effort: str
) -> dict[str, Any]:
    expected = f"DIRECT-BENCH-{index}"
    prompt = f"Reply exactly {expected}. Do not use tools."
    command = [
        binary,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        prompt,
    ]
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout and process.stderr
    ready_at = None
    first_output_at = None
    answer = None
    while line := await process.stdout.readline():
        event = json.loads(line)
        if event.get("type") == "thread.started" and ready_at is None:
            ready_at = time.perf_counter() - started
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            first_output_at = first_output_at or time.perf_counter() - started
            answer = item.get("text")
    stderr = (await process.stderr.read()).decode(errors="replace")
    returncode = await process.wait()
    total = time.perf_counter() - started
    return {
        "path": "direct",
        "sample": index,
        "ready_s": ready_at,
        "first_output_s": first_output_at,
        "total_s": total,
        "correct": returncode == 0 and answer == expected,
        "returncode": returncode,
        "stderr_tail": stderr[-300:] if returncode else "",
    }


async def _app_server_sample(
    *, index: int, server: CodexAppServer, cwd: Path, model: str, effort: str
) -> dict[str, Any]:
    expected = f"APP-BENCH-{index}"
    prompt = f"Reply exactly {expected}. Do not use tools."
    started = time.perf_counter()
    process, _ = await server.start_turn(
        prompt=prompt,
        cwd=str(cwd),
        model=model,
        effort=effort,
        resume_session_id=None,
        git_env=None,
        task_id=None,
    )
    ready_at = time.perf_counter() - started
    first_output_at = None
    answer = None
    while line := await process.stdout.readline():
        event = json.loads(line)
        if (
            event.get("type") == "item.agent_message.delta"
            and event.get("delta")
            and first_output_at is None
        ):
            first_output_at = time.perf_counter() - started
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = item.get("text")
    returncode = await process.wait()
    return {
        "path": "app_server",
        "sample": index,
        "ready_s": ready_at,
        "first_output_s": first_output_at,
        "total_s": time.perf_counter() - started,
        "correct": returncode == 0 and answer == expected,
        "returncode": returncode,
        "stderr_tail": "",
    }


def _summary(samples: list[dict[str, Any]], path: str) -> dict[str, Any]:
    rows = [row for row in samples if row["path"] == path]

    def median(field: str) -> float:
        return round(statistics.median(row[field] for row in rows), 3)

    return {
        "path": path,
        "samples": len(rows),
        "ready_median_s": median("ready_s"),
        "first_output_median_s": median("first_output_s"),
        "total_median_s": median("total_s"),
        "all_correct": all(row["correct"] for row in rows),
    }


async def _run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    server = CodexAppServer(args.binary, request_timeout=args.request_timeout)
    samples: list[dict[str, Any]] = []
    try:
        # Keep process startup out of the warm-path measurements. The first
        # user request in production remains a cold start and is reported by
        # the app-server's own startup_ms log.
        await server.ensure_started()
        for index in range(args.samples):
            samples.append(await _direct_sample(
                index=index,
                binary=args.binary,
                cwd=cwd,
                model=args.model,
                effort=args.effort,
            ))
            samples.append(await _app_server_sample(
                index=index,
                server=server,
                cwd=cwd,
                model=args.model,
                effort=args.effort,
            ))
    finally:
        await server.shutdown()

    rounded = [
        {
            **row,
            **{
                field: round(row[field], 3) if row[field] is not None else None
                for field in ("ready_s", "first_output_s", "total_s")
            },
        }
        for row in samples
    ]
    summaries = [_summary(samples, "direct"), _summary(samples, "app_server")]
    print(json.dumps({"samples": rounded, "summary": summaries}, ensure_ascii=False))
    return 0 if all(row["correct"] for row in samples) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--binary", default="codex")
    parser.add_argument("--cwd", default=str(PROJECT_ROOT))
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be at least 1")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
