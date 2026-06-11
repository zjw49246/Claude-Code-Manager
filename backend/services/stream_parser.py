import json
from datetime import datetime


class StreamParser:
    """Parse Claude Code stream-json (NDJSON) output into structured events."""

    def parse_line(self, line: str) -> list[dict]:
        """Parse a single NDJSON line into one or more events.

        Returns a list because a single assistant/user event may contain
        multiple content blocks (e.g. text + tool_use), each yielding a
        separate event.
        """
        if not line.strip():
            return []
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return [{
                "event_type": "parse_error",
                "content": line,
                "is_error": True,
                "timestamp": datetime.utcnow().isoformat(),
            }]

        event_type = data.get("type", "unknown")
        now = datetime.utcnow().isoformat()

        def _base_event() -> dict:
            return {
                "event_type": event_type,
                "role": data.get("role"),
                "content": self._extract_content(data),
                "tool_name": None,
                "tool_input": None,
                "tool_output": None,
                "raw_json": line,
                "is_error": False,
                "timestamp": now,
            }

        # Extract session_id from system/init or result events
        if event_type == "system" and data.get("subtype") == "init":
            event = _base_event()
            event["session_id"] = data.get("session_id")
            event["event_type"] = "system_init"
            return [event]
        elif event_type == "system":
            subtype = data.get("subtype", "system")
            # Skip noisy telemetry subtypes that flood the chat UI
            _SKIP_SUBTYPES = {"thinking_tokens", "token_usage", "api_request", "api_response"}
            if subtype in _SKIP_SUBTYPES:
                return []
            event = _base_event()
            event["event_type"] = "system_event"
            event["content"] = subtype
            return [event]
        elif event_type == "assistant":
            # Extract token usage from assistant message for context window tracking
            usage_data = None
            message_obj = data.get("message", {}) if isinstance(data.get("message"), dict) else {}
            usage = message_obj.get("usage")
            if isinstance(usage, dict):
                input_tokens = usage.get("input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_creation = usage.get("cache_creation_input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                usage_data = {
                    "input_tokens": input_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                    "output_tokens": output_tokens,
                    "total_input_tokens": input_tokens + cache_read + cache_creation,
                }

            # Parse ALL content blocks — one event per block
            content_blocks = message_obj.get("content", []) if message_obj else data.get("content", [])
            if not isinstance(content_blocks, list):
                event = _base_event()
                event["role"] = "assistant"
                event["event_type"] = "message"
                if usage_data:
                    event["context_usage"] = usage_data
                return [event]
            events = []
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                evt = _base_event()
                evt["role"] = "assistant"
                if block.get("type") == "tool_use":
                    evt["event_type"] = "tool_use"
                    evt["tool_name"] = block.get("name")
                    evt["tool_input"] = json.dumps(block.get("input", {}))
                    evt["content"] = None
                    events.append(evt)
                elif block.get("type") == "thinking":
                    evt["event_type"] = "thinking"
                    evt["content"] = self._extract_thinking_text(block)
                    events.append(evt)
                elif block.get("type") == "text":
                    evt["event_type"] = "message"
                    evt["content"] = block.get("text", "")
                    events.append(evt)
            if not events:
                event = _base_event()
                event["role"] = "assistant"
                event["event_type"] = "message"
                if usage_data:
                    event["context_usage"] = usage_data
                return [event]
            # Attach usage data to the first event only
            if usage_data and events:
                events[0]["context_usage"] = usage_data
            return events
        elif event_type == "user":
            # Claude Code sends tool results as type: "user" with tool_result content blocks
            msg_content = data.get("message", {}).get("content", []) if isinstance(data.get("message"), dict) else []
            if isinstance(msg_content, list):
                events = []
                for block in msg_content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        evt = _base_event()
                        evt["event_type"] = "tool_result"
                        evt["role"] = "tool"
                        # content can be a string or a list of content blocks
                        raw_content = block.get("content", "")
                        if isinstance(raw_content, list):
                            texts = [b.get("text", "") for b in raw_content if isinstance(b, dict) and b.get("type") == "text"]
                            evt["tool_output"] = "\n".join(texts) if texts else str(raw_content)
                        else:
                            evt["tool_output"] = raw_content
                        if block.get("is_error"):
                            evt["is_error"] = True
                        events.append(evt)
                if events:
                    return events
            event = _base_event()
            event["event_type"] = "tool_result"
            event["role"] = "tool"
            return [event]
        elif event_type == "tool_use":
            event = _base_event()
            event["tool_name"] = data.get("name")
            event["tool_input"] = json.dumps(data.get("input", {}))
            return [event]
        elif event_type == "tool_result":
            event = _base_event()
            event["tool_output"] = self._extract_content(data) or ""
            if isinstance(event["tool_output"], str) and "error" in event["tool_output"].lower():
                event["is_error"] = True
            return [event]
        elif event_type == "result":
            event = _base_event()
            event["content"] = self._extract_content(data)
            event["session_id"] = data.get("session_id")
            cost = data.get("total_cost_usd")
            if cost is not None:
                event["cost_usd"] = cost
            # NOTE: result.usage is CUMULATIVE across all API requests of the
            # run (observed 45x the real context size on long tool-use turns)
            # — never use it for the context indicator. The per-request
            # assistant events already carry accurate usage; only forward the
            # authoritative context_window here.
            model_usage = data.get("modelUsage")
            if isinstance(model_usage, dict):
                for _model_name, model_data in model_usage.items():
                    if isinstance(model_data, dict) and "contextWindow" in model_data:
                        event["context_usage"] = {
                            "context_window": model_data["contextWindow"],
                        }
                        break
            if data.get("is_error"):
                event["is_error"] = True
            return [event]
        else:
            return [_base_event()]

    def _extract_thinking_text(self, block: dict) -> str:
        """Extract thinking text from a thinking content block.

        Newer Claude Code / API versions vary the field name and may return
        encrypted thinking (no plaintext at all). Try the known field names in
        order, and if only an encrypted payload is present, return a marker so
        the UI can communicate the situation rather than rendering an empty box.
        """
        for key in ("thinking", "text", "content", "summary"):
            value = block.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list):
                texts = [
                    b.get("text", "")
                    for b in value
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(t for t in texts if t)
                if joined:
                    return joined
        # Encrypted thinking blocks carry only `signature` + `data` (no plaintext).
        if block.get("signature") or block.get("data"):
            return "[encrypted thinking — no plaintext returned by the API]"
        return ""

    def _extract_content(self, data: dict) -> str | None:
        # Handle content blocks (list of {type, text})
        content = data.get("content")
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(texts) if texts else None
        if isinstance(content, str):
            return content
        # Handle message wrapper
        message = data.get("message")
        if isinstance(message, dict):
            return self._extract_content(message)
        return None
