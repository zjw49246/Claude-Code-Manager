"""仓库版本信息（Manager/Worker 版本锁定用）。

cwd 固定到本仓库根（从 __file__ 推导），不依赖进程启动目录——
systemd 的 WorkingDirectory 配错时 `git rev-parse` 静默返回错值的坑。
"""

import os
import subprocess

# backend/services/git_info.py -> 仓库根
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def git_head_commit(cwd: str = REPO_ROOT) -> str:
    """返回 HEAD commit（失败返回 ""，不抛异常）。同步调用，启动期/线程内使用。

    git 不可用时回退读 .deploy_commit——Worker 部署走 rsync 不带 .git
    （从 worktree 部署时 .git 只是个指向 Manager 本地路径的悬空指针文件），
    由 provisioner 在部署时写入该文件。
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    try:
        with open(os.path.join(cwd, ".deploy_commit")) as f:
            return f.read().strip()
    except Exception:
        return ""
