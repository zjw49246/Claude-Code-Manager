"""归并所有账号的 session 文件到第一个账号的 config_dir（~/.claude）。

设计文档 §13：pool.select() 按 cooldown 选号，session 可能只存在于
account-N 的 config_dir。迁移/销毁前先归并，rsync 第一个账号即可拿全。
在 task 所在机器上执行（Manager 本地直接跑，Worker 上经 SSH 跑）。
"""

import json
import os
from pathlib import Path


def consolidate() -> int:
    first_dir = Path.home() / ".claude"
    first_projects = first_dir / "projects"
    linked = 0

    # 候选：pool accounts.json 列出的 config_dir + ~/.claude-* 通配兜底
    candidates: list[Path] = []
    pool_path = Path.home() / ".claude-pool" / "accounts.json"
    if pool_path.exists():
        try:
            data = json.loads(pool_path.read_text())
            for account in data.get("accounts", []):
                if account.get("config_dir"):
                    candidates.append(Path(os.path.expanduser(account["config_dir"])))
        except (json.JSONDecodeError, OSError):
            pass
    candidates.extend(Path.home().glob(".claude-account-*"))

    seen: set[Path] = set()
    for other_dir in candidates:
        other_dir = other_dir.resolve()
        if other_dir in seen or other_dir == first_dir.resolve():
            continue
        seen.add(other_dir)
        other_projects = other_dir / "projects"
        if not other_projects.is_dir():
            continue
        for session_file in other_projects.glob("*/*.jsonl"):
            rel = session_file.relative_to(other_projects)
            target = first_projects / rel
            if target.exists():
                continue  # 已存在（可能已硬链接），不覆盖
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(session_file, target)
            except OSError:
                import shutil
                shutil.copy2(session_file, target)
            linked += 1
            print(f"consolidated: {session_file} -> {target}")
    print(f"done, {linked} session(s) consolidated")
    return linked


if __name__ == "__main__":
    consolidate()
