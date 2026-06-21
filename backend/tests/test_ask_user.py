"""Tests for ask_user：拦截 AskUserQuestion → 前端卡片 → 答案喂回模型。

覆盖纯逻辑（registry / format / settings 注入）；完整 HTTP+claude 回环由
集成测试在真实环境验证（见 PROGRESS.md task ask_user）。
"""
import asyncio
import json

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
