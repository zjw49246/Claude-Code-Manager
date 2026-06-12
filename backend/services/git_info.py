"""仓库版本信息（Manager/Worker 版本锁定用）。

cwd 固定到本仓库根（从 __file__ 推导），不依赖进程启动目录——
systemd 的 WorkingDirectory 配错时 `git rev-parse` 静默返回错值的坑。
"""

import os
import subprocess

# backend/services/git_info.py -> 仓库根
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def git_head_commit(cwd: str = REPO_ROOT) -> str:
    """返回 HEAD commit（失败返回 ""，不抛异常）。同步调用，启动期/线程内使用。"""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""
