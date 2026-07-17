"""Tests for ask_user：拦截 AskUserQuestion → 前端卡片 → 答案喂回模型。

覆盖纯逻辑（registry / format / settings 注入）+ hook 脚本决策输出
（subprocess + 单次 stub HTTP server）；完整 HTTP+claude 回环由
集成测试在真实环境验证（见 PROGRESS.md task ask_user）。
"""
import asyncio
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from backend.services.ask_user import (
    AskUserRegistry,
    format_answer_reason,
)
from backend.services.ask_user_settings import (
    ensure_ask_user_hook,
    _is_our_pretool_entry,
    _MATCHER,
    _MARKER,
)


# ----------------------------------------------------------------- registry

@pytest.mark.asyncio
async def test_registry_create_resolve_roundtrip():
    reg = AskUserRegistry()
    pending = reg.create(task_id=7, session_id="sid", questions=[{"question": "?"}])
    assert pending.request_id
    assert reg.get(pending.request_id) is pending
    assert reg.list_for_task(7) == [pending]
    assert reg.list_for_task(8) == []

    answers = [{"labels": ["A"], "text": ""}]
    assert reg.resolve(pending.request_id, answers) is True
    assert await pending.future == answers


@pytest.mark.asyncio
async def test_registry_resolve_unknown_and_double():
    reg = AskUserRegistry()
    assert reg.resolve("nope", []) is False
    pending = reg.create(task_id=1, session_id="s", questions=[{"question": "?"}])
    assert reg.resolve(pending.request_id, [{"labels": ["x"]}]) is True
    # second resolve must fail (future already done)
    assert reg.resolve(pending.request_id, [{"labels": ["y"]}]) is False


@pytest.mark.asyncio
async def test_registry_discard_and_list_excludes_done():
    reg = AskUserRegistry()
    p1 = reg.create(task_id=3, session_id="s", questions=[{"question": "?"}])
    p2 = reg.create(task_id=3, session_id="s", questions=[{"question": "?"}])
    assert len(reg.list_for_task(3)) == 2
    reg.resolve(p1.request_id, [])
    # resolved (future done) → excluded from pending list
    assert reg.list_for_task(3) == [p2]
    reg.discard(p2.request_id)
    assert reg.get(p2.request_id) is None


@pytest.mark.asyncio
async def test_registry_list_all_spans_tasks_and_excludes_done():
    reg = AskUserRegistry()
    a = reg.create(task_id=1, session_id="s", questions=[{"question": "?"}])
    b = reg.create(task_id=2, session_id="s", questions=[{"question": "?"}])
    # list_all 跨 task 汇总（驱动全局通知）
    assert {p.request_id for p in reg.list_all()} == {a.request_id, b.request_id}
    reg.resolve(a.request_id, [])
    # 已回答（future done）从全局列表剔除
    assert [p.request_id for p in reg.list_all()] == [b.request_id]


# ------------------------------------------------------------------- format

def test_format_answer_reason_single_select():
    questions = [{
        "question": "Tabs or spaces?",
        "header": "Indent",
        "options": [
            {"label": "Tabs", "description": "tab chars"},
            {"label": "Spaces", "description": "space chars"},
        ],
        "multiSelect": False,
    }]
    reason = format_answer_reason(questions, [{"labels": ["Spaces"], "text": ""}])
    assert "Tabs or spaces?" in reason
    assert "Spaces (space chars)" in reason
    assert "Do NOT call AskUserQuestion again" in reason


def test_format_answer_reason_multiselect_and_custom_text():
    questions = [{
        "question": "Pick langs",
        "options": [{"label": "Py"}, {"label": "Go"}, {"label": "Rust"}],
        "multiSelect": True,
    }]
    reason = format_answer_reason(questions, [{"labels": ["Py", "Rust"], "text": "also C"}])
    assert "Py" in reason and "Rust" in reason
    assert 'also C' in reason


def test_format_answer_reason_missing_answer():
    questions = [{"question": "Q1", "options": []}, {"question": "Q2", "options": []}]
    # only one answer provided for two questions → no crash, '(no selection)'
    reason = format_answer_reason(questions, [{"labels": ["x"]}])
    assert "Q1" in reason and "Q2" in reason
    assert "(no selection)" in reason


# ----------------------------------------------------------- settings inject

def _set_enabled(value: bool):
    from backend.config import settings
    settings.ask_user_enabled = value


def test_inject_adds_hook_and_is_idempotent(tmp_path):
    _set_enabled(True)
    sp = tmp_path / "settings.json"
    sp.write_text(json.dumps({
        "theme": "dark",
        "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo x"}]}
        ]},
    }))

    ensure_ask_user_hook(str(tmp_path))
    d1 = json.loads(sp.read_text())
    pre = d1["hooks"]["PreToolUse"]
    # 保留原有 key 与其它 hook
    assert d1["theme"] == "dark"
    assert any(e.get("matcher") == "Bash" for e in pre)
    ours = [e for e in pre if _is_our_pretool_entry(e)]
    assert len(ours) == 1
    assert ours[0]["matcher"] == _MATCHER
    assert _MARKER in ours[0]["hooks"][0]["command"]

    # 第二次注入不重复
    ensure_ask_user_hook(str(tmp_path))
    d2 = json.loads(sp.read_text())
    ours2 = [e for e in d2["hooks"]["PreToolUse"] if _is_our_pretool_entry(e)]
    assert len(ours2) == 1


def test_disable_removes_our_hook_only(tmp_path):
    _set_enabled(True)
    ensure_ask_user_hook(str(tmp_path))
    sp = tmp_path / "settings.json"
    # add an unrelated hook alongside
    data = json.loads(sp.read_text())
    data["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo x"}]}
    )
    sp.write_text(json.dumps(data))

    try:
        _set_enabled(False)
        ensure_ask_user_hook(str(tmp_path))
    finally:
        _set_enabled(True)

    d = json.loads(sp.read_text())
    pre = d.get("hooks", {}).get("PreToolUse", [])
    assert not any(_is_our_pretool_entry(e) for e in pre)
    assert any(e.get("matcher") == "Bash" for e in pre)


def test_inject_handles_corrupt_settings(tmp_path):
    _set_enabled(True)
    sp = tmp_path / "settings.json"
    sp.write_text("{ not valid json ")
    ensure_ask_user_hook(str(tmp_path))  # must not raise
    d = json.loads(sp.read_text())
    assert any(_is_our_pretool_entry(e) for e in d["hooks"]["PreToolUse"])


def test_inject_creates_missing_dir(tmp_path):
    _set_enabled(True)
    target = tmp_path / "newconf"
    ensure_ask_user_hook(str(target))
    sp = target / "settings.json"
    assert sp.exists()
    d = json.loads(sp.read_text())
    assert any(_is_our_pretool_entry(e) for e in d["hooks"]["PreToolUse"])


def test_inject_hook_carries_cli_timeout(tmp_path):
    """hook 项必须带显式 timeout：CLI 默认 600s 杀 hook 命令，会把 /wait
    阻塞中的 hook 杀掉 → fail-open 弹原生交互框冻死 PTY turn（task 32）。"""
    from backend.config import settings

    _set_enabled(True)
    ensure_ask_user_hook(str(tmp_path))
    d = json.loads((tmp_path / "settings.json").read_text())
    ours = [e for e in d["hooks"]["PreToolUse"] if _is_our_pretool_entry(e)]
    assert ours[0]["hooks"][0]["timeout"] == int(settings.ask_user_timeout) + 60


# -------------------------------------------------------------- hook script

_HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "hooks" / "ask_user_hook.py"


def _run_hook_against(response_body: dict) -> subprocess.CompletedProcess:
    """跑真实 hook 脚本，stub 后端返回给定 /wait 响应。"""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps(response_body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静音测试输出
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        payload = json.dumps({
            "tool_name": "AskUserQuestion",
            "session_id": "sid-hook-test",
            "tool_input": {"questions": [{"question": "?"}]},
        })
        return subprocess.run(
            [sys.executable, str(_HOOK_SCRIPT),
             "--api-base", f"http://127.0.0.1:{srv.server_address[1]}",
             "--timeout", "10"],
            input=payload, capture_output=True, text=True, timeout=30,
        )
    finally:
        srv.shutdown()


def test_hook_script_answer_feeds_deny_reason():
    cp = _run_hook_against({"answered": True, "reason": "The user picked A."})
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "The user picked A."


def test_hook_script_timed_out_denies_not_fail_open():
    """超时不放行：PTY 下原生 AskUserQuestion 会冻死 turn（task 32, 2026-07-17）。"""
    cp = _run_hook_against({"answered": False, "timed_out": True})
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "best judgment" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_script_no_session_fails_open():
    cp = _run_hook_against({"answered": False, "no_session": True})
    assert cp.returncode == 0
    assert cp.stdout.strip() == ""  # 无决策输出 = 放行原生工具
