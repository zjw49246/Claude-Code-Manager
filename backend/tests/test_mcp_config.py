"""Tests for MCP config generation and cleanup."""
import json
import sys
import tempfile
from pathlib import Path

import pytest

from backend.services.mcp_config import generate_mcp_config, cleanup_mcp_config


def test_generate_mcp_config_none_skills():
    assert generate_mcp_config(1, None, api_base="http://localhost:8000") is None


def test_generate_mcp_config_empty_skills():
    assert generate_mcp_config(1, {}, api_base="http://localhost:8000") is None


def test_generate_mcp_config_no_matching_skills():
    assert generate_mcp_config(1, {"worker": True}, api_base="http://localhost:8000") is None


def test_generate_mcp_config_monitor_enabled():
    path = generate_mcp_config(99, {"monitor": True}, api_base="http://test:8000")
    assert path is not None
    assert path.exists()

    config = json.loads(path.read_text())
    assert "mcpServers" in config
    assert "ccm_skills" in config["mcpServers"]

    server = config["mcpServers"]["ccm_skills"]
    assert server["command"] == sys.executable
    assert "--task-id" in server["args"]
    assert "99" in server["args"]
    assert "--api-base" in server["args"]
    assert "http://test:8000" in server["args"]
    assert "-m" in server["args"]
    assert "backend.mcp.ccm_skills_server" in server["args"]

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
