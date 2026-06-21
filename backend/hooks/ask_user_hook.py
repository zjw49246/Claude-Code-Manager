#!/usr/bin/env python3
"""PreToolUse hook：拦截内置 AskUserQuestion，转 CCM 前端卡片，把答案喂回模型。

由 CCM 在每次 launch 时注入到 {config_dir}/settings.json 的 PreToolUse hook 调用
（matcher=AskUserQuestion）。stdin 收到 Claude Code 的 hook payload：
  {session_id, cwd, tool_name, tool_input:{questions:[...]}, tool_use_id, ...}

流程：
  1. 阻塞式 POST {api_base}/api/ask-user/wait（带 questions + session_id），
     CCM 广播卡片、等用户在前端回答；
  2. 拿到 {answered:true, reason} → 打印 PreToolUse deny + permissionDecisionReason，
     deny 的 reason 会作为 tool_result（is_error）喂回模型，模型据此当作"用户回答"继续；
  3. 任何失败/超时/非 CCM session → 不输出（exit 0），放行原生 AskUserQuestion 兜底，
     绝不因 CCM 不可用而打断会话。

仅用标准库（urllib），不依赖 httpx，任何 python3 都能跑。
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def _fail_open(msg: str = "") -> None:
    """放行原生工具：不打印任何决策，exit 0。"""
    if msg:
        print(msg, file=sys.stderr)
    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--auth-token", default="")
    parser.add_argument("--timeout", type=int, default=1900)
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except Exception as e:  # noqa: BLE001
        _fail_open(f"ask_user_hook: bad stdin: {e}")
        return

    if payload.get("tool_name") != "AskUserQuestion":
        _fail_open()
        return

    tool_input = payload.get("tool_input") or {}
    questions = tool_input.get("questions") or []
    session_id = payload.get("session_id") or ""
    if not questions or not session_id:
        _fail_open("ask_user_hook: missing questions/session_id")
        return

    body = json.dumps({
        "session_id": session_id,
        "cwd": payload.get("cwd"),
        "tool_use_id": payload.get("tool_use_id"),
        "questions": questions,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{args.api_base.rstrip('/')}/api/ask-user/wait",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if args.auth_token:
        req.add_header("Authorization", f"Bearer {args.auth_token}")

    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        _fail_open(f"ask_user_hook: CCM unreachable: {e}")
        return
    except Exception as e:  # noqa: BLE001
        _fail_open(f"ask_user_hook: error: {e}")
        return

    if not data.get("answered"):
        # 超时 / 非 CCM session → 放行原生工具
        _fail_open(f"ask_user_hook: not answered ({data})")
        return

    reason = data.get("reason") or "The user has responded via the UI; continue accordingly."
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
