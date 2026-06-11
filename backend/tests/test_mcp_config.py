"""Tests for MCP config generation and cleanup."""
import json
import tempfile
from pathlib import Path

import pytest

from backend.services.mcp_config import generate_mcp_config, cleanup_mcp_config


def _assert_ccm_skills_config(path, task_id: int, api_base: str):
    """ccm_skills server 始终包含，参数齐全。"""
    assert path is not None
    assert path.exists()

    config = json.loads(path.read_text())
    assert "mcpServers" in config
    assert "ccm_skills" in config["mcpServers"]

    server = config["mcpServers"]["ccm_skills"]
    assert server["command"].endswith("python3")
    assert "--task-id" in server["args"]
    assert str(task_id) in server["args"]
    assert "--api-base" in server["args"]
    assert api_base in server["args"]
    assert "-m" in server["args"]
    assert "backend.mcp.ccm_skills_server" in server["args"]


def test_generate_mcp_config_none_skills_still_includes_ccm_skills():
    """skills=None 时也返回配置：ccm_skills 提供 $help 等默认命令。"""
    path = generate_mcp_config(1, None, api_base="http://localhost:8000")
    _assert_ccm_skills_config(path, 1, "http://localhost:8000")
    path.unlink(missing_ok=True)


def test_generate_mcp_config_empty_skills_still_includes_ccm_skills():
    """skills={} 时也返回配置（ccm_skills 始终包含）。"""
    path = generate_mcp_config(1, {}, api_base="http://localhost:8000")
    _assert_ccm_skills_config(path, 1, "http://localhost:8000")
    path.unlink(missing_ok=True)


def test_generate_mcp_config_skills_do_not_add_extra_servers():
    """启用任意 skill 不再产生独立的 per-skill server，只有 ccm_skills 一个入口。"""
    path = generate_mcp_config(1, {"worker": True, "monitor": True}, api_base="http://localhost:8000")
    config = json.loads(path.read_text())
    assert set(config["mcpServers"].keys()) == {"ccm_skills"}
    path.unlink(missing_ok=True)


def test_generate_mcp_config_monitor_enabled():
    path = generate_mcp_config(99, {"monitor": True}, api_base="http://test:8000")
    _assert_ccm_skills_config(path, 99, "http://test:8000")
    path.unlink(missing_ok=True)


def test_generate_mcp_config_file_path():
    path = generate_mcp_config(42, {"monitor": True}, api_base="http://localhost:8000")
    expected = Path(tempfile.gettempdir()) / "ccm_mcp_42.json"
    assert path == expected
    path.unlink(missing_ok=True)


def test_cleanup_mcp_config():
    path = generate_mcp_config(77, {"monitor": True}, api_base="http://localhost:8000")
    assert path.exists()
    cleanup_mcp_config(77)
    assert not path.exists()


def test_cleanup_mcp_config_missing_file():
    cleanup_mcp_config(999999)
