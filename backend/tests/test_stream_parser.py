"""Tests for StreamParser — NDJSON line parsing."""
import json
import pytest

from backend.services.stream_parser import StreamParser


@pytest.fixture
def parser():
    return StreamParser()


def test_empty_line(parser):
    assert parser.parse_line("") == []
    assert parser.parse_line("   ") == []


def test_invalid_json(parser):
    results = parser.parse_line("not json at all")
    assert len(results) == 1
    result = results[0]
    assert result["event_type"] == "parse_error"
    assert result["is_error"] is True
    assert result["content"] == "not json at all"


def test_system_init(parser):
    line = json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": "abc-123",
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "system_init"
    assert result["session_id"] == "abc-123"


def test_rate_limit_event_surfaces_info(parser):
    line = json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed_warning",
            "rateLimitType": "seven_day",
            "utilization": 0.37,
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "rate_limit_event"
    assert result["rate_limit_info"]["status"] == "allowed_warning"
    assert result["rate_limit_info"]["rateLimitType"] == "seven_day"


def test_rate_limit_event_missing_info(parser):
    result = parser.parse_line(json.dumps({"type": "rate_limit_event"}))[0]
    assert result["event_type"] == "rate_limit_event"
    assert result["rate_limit_info"] is None


def test_assistant_message(parser):
    line = json.dumps({
        "type": "assistant",
        "content": [{"type": "text", "text": "Hello world"}],
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "message"
    assert result["role"] == "assistant"
    assert result["content"] == "Hello world"


def test_tool_use(parser):
    line = json.dumps({
        "type": "tool_use",
        "name": "Read",
        "input": {"file_path": "/tmp/test.py"},
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "tool_use"
    assert result["tool_name"] == "Read"
    assert '"file_path"' in result["tool_input"]


def test_tool_result(parser):
    line = json.dumps({
        "type": "tool_result",
        "content": "file contents here",
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "tool_result"
    assert result["tool_output"] == "file contents here"
    assert result["is_error"] is False


def test_tool_result_error(parser):
    line = json.dumps({
        "type": "tool_result",
        "content": "Error: file not found",
    })
    result = parser.parse_line(line)[0]
    assert result["is_error"] is True


def test_result_with_cost(parser):
    line = json.dumps({
        "type": "result",
        "session_id": "sess-456",
        "total_cost_usd": 0.42,
        "content": [{"type": "text", "text": "Done"}],
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "result"
    assert result["session_id"] == "sess-456"
    assert result["cost_usd"] == 0.42
    assert result["content"] == "Done"


def test_result_is_error(parser):
    line = json.dumps({
        "type": "result",
        "is_error": True,
        "content": "Something failed",
    })
    result = parser.parse_line(line)[0]
    assert result["is_error"] is True


def test_content_extraction_string(parser):
    line = json.dumps({"type": "unknown", "content": "plain string"})
    result = parser.parse_line(line)[0]
    assert result["content"] == "plain string"


def test_content_extraction_list(parser):
    line = json.dumps({
        "type": "unknown",
        "content": [
            {"type": "text", "text": "line 1"},
            {"type": "text", "text": "line 2"},
        ],
    })
    result = parser.parse_line(line)[0]
    assert result["content"] == "line 1\nline 2"


def test_content_extraction_empty_list(parser):
    line = json.dumps({"type": "unknown", "content": []})
    result = parser.parse_line(line)[0]
    assert result["content"] is None


def test_content_extraction_message_wrapper(parser):
    line = json.dumps({
        "type": "unknown",
        "message": {"content": [{"type": "text", "text": "nested"}]},
    })
    result = parser.parse_line(line)[0]
    assert result["content"] == "nested"


def test_assistant_tool_use_block(parser):
    """assistant event with tool_use content block → tool_use event."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_123", "name": "Bash", "input": {"command": "ls -la"}}],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "tool_use"
    assert result["tool_name"] == "Bash"
    assert '"command"' in result["tool_input"]
    assert result["role"] == "assistant"


def test_assistant_thinking_block(parser):
    """assistant event with thinking content block → thinking event."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "Let me analyze this..."}],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "thinking"
    assert result["content"] == "Let me analyze this..."
    assert result["role"] == "assistant"


def test_user_event_tool_result(parser):
    """type: 'user' event with tool_result content → tool_result event."""
    line = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_123", "content": "file contents here", "is_error": False}],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "tool_result"
    assert result["role"] == "tool"
    assert result["tool_output"] == "file contents here"
    assert result["is_error"] is False


def test_user_event_tool_result_error(parser):
    """type: 'user' event with is_error flag → is_error set."""
    line = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_456", "content": "Error: not found", "is_error": True}],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "tool_result"
    assert result["is_error"] is True


def test_system_non_init(parser):
    """system event with non-init subtype → system_event."""
    line = json.dumps({
        "type": "system",
        "subtype": "task_started",
        "task_id": "abc",
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "system_event"
    assert result["content"] == "task_started"


def test_assistant_empty_content_blocks(parser):
    """assistant event with empty content blocks → message event."""
    line = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": []},
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "message"


def test_assistant_multiple_content_blocks(parser):
    """assistant event with text + tool_use blocks → multiple events."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me edit the file."},
                {"type": "tool_use", "id": "toolu_789", "name": "Edit", "input": {"file_path": "/tmp/a.py", "old_string": "foo", "new_string": "bar"}},
            ],
        },
    })
    results = parser.parse_line(line)
    assert len(results) == 2
    assert results[0]["event_type"] == "message"
    assert results[0]["content"] == "Let me edit the file."
    assert results[1]["event_type"] == "tool_use"
    assert results[1]["tool_name"] == "Edit"


def test_user_multiple_tool_results(parser):
    """user event with multiple tool_result blocks → multiple events."""
    line = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result 1"},
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": "result 2", "is_error": True},
            ],
        },
    })
    results = parser.parse_line(line)
    assert len(results) == 2
    assert results[0]["tool_output"] == "result 1"
    assert results[0]["is_error"] is False
    assert results[1]["tool_output"] == "result 2"
    assert results[1]["is_error"] is True


# ── Context window usage tests ─────────────────────────────────────────────────

def test_assistant_message_with_usage(parser):
    """assistant event with message.usage → context_usage attached to first event."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi!"}],
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 200,
                "output_tokens": 30,
            },
        },
    })
    results = parser.parse_line(line)
    assert len(results) == 1
    result = results[0]
    assert result["event_type"] == "message"
    assert "context_usage" in result
    cu = result["context_usage"]
    assert cu["input_tokens"] == 100
    assert cu["cache_read_input_tokens"] == 500
    assert cu["cache_creation_input_tokens"] == 200
    assert cu["output_tokens"] == 30
    assert cu["total_input_tokens"] == 800  # 100 + 500 + 200


def test_assistant_tool_use_with_usage(parser):
    """assistant tool_use event with message.usage → context_usage on first event."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
            "usage": {
                "input_tokens": 50,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 0,
                "output_tokens": 10,
            },
        },
    })
    results = parser.parse_line(line)
    assert len(results) == 1
    assert results[0]["event_type"] == "tool_use"
    cu = results[0]["context_usage"]
    assert cu["total_input_tokens"] == 350  # 50 + 300 + 0
    assert cu["output_tokens"] == 10


def test_assistant_multiple_blocks_usage_on_first_only(parser):
    """With multiple content blocks, context_usage only on first event."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me help."},
                {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/tmp/f"}},
            ],
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 5,
                "output_tokens": 8,
            },
        },
    })
    results = parser.parse_line(line)
    assert len(results) == 2
    assert "context_usage" in results[0]
    assert "context_usage" not in results[1]
    assert results[0]["context_usage"]["total_input_tokens"] == 15  # 10 + 0 + 5


def test_assistant_message_no_usage(parser):
    """assistant event without usage field → no context_usage key."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello"}],
        },
    })
    result = parser.parse_line(line)[0]
    assert "context_usage" not in result


def test_result_with_model_usage(parser):
    """result event with modelUsage → context_usage carries ONLY context_window.

    Regression: result.usage is cumulative across all API requests of the run
    (observed 45x the real context size on long tool-use turns), so the token
    numbers must never be forwarded from here — per-request assistant events
    are the accurate source.
    """
    line = json.dumps({
        "type": "result",
        "session_id": "sess-789",
        "total_cost_usd": 0.05,
        "usage": {
            # Cumulative run totals — NOT the current context size
            "input_tokens": 3,
            "cache_read_input_tokens": 868698,
            "cache_creation_input_tokens": 511488,
            "output_tokens": 5,
        },
        "modelUsage": {
            "claude-opus-4-6[1m]": {
                "inputTokens": 3,
                "outputTokens": 5,
                "cacheReadInputTokens": 868698,
                "cacheCreationInputTokens": 511488,
                "contextWindow": 1000000,
                "maxOutputTokens": 64000,
                "costUSD": 0.05,
            }
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "result"
    assert result["cost_usd"] == 0.05
    assert "context_usage" in result
    cu = result["context_usage"]
    assert cu == {"context_window": 1000000}
    assert "total_input_tokens" not in cu


def test_result_without_model_usage(parser):
    """result event without modelUsage → no context_usage key."""
    line = json.dumps({
        "type": "result",
        "session_id": "sess-000",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "result"
    assert "context_usage" not in result


def test_result_model_usage_no_context_window(parser):
    """result event with modelUsage but missing contextWindow → no context_usage."""
    line = json.dumps({
        "type": "result",
        "session_id": "sess-111",
        "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {
            "some-model": {"inputTokens": 5, "outputTokens": 2}
        },
    })
    result = parser.parse_line(line)[0]
    assert "context_usage" not in result


# ── Thinking block fallback tests (Opus 4.7+ field-name variations) ─────────────

def test_thinking_with_text_field(parser):
    """Some Claude Code versions emit thinking under `text` instead of `thinking`."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "text": "Reasoning via text field"}],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "thinking"
    assert result["content"] == "Reasoning via text field"


def test_thinking_with_nested_content_blocks(parser):
    """thinking block whose `content` is a list of text sub-blocks."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "content": [
                        {"type": "text", "text": "step 1"},
                        {"type": "text", "text": "step 2"},
                    ],
                }
            ],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "thinking"
    assert result["content"] == "step 1\nstep 2"


def test_thinking_encrypted_block(parser):
    """Encrypted thinking (only signature/data, no plaintext) → marker string."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "thinking",
                "signature": "sig-abc",
                "data": "base64payload==",
            }],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "thinking"
    assert result["content"].startswith("[encrypted thinking")


def test_thinking_completely_empty_block(parser):
    """thinking block with no recognised text fields and no encryption → empty content."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking"}],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["event_type"] == "thinking"
    assert result["content"] == ""


def test_thinking_legacy_field_still_works(parser):
    """Existing `thinking` field must still be the primary path."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "primary text"}],
        },
    })
    result = parser.parse_line(line)[0]
    assert result["content"] == "primary text"
