"""Agent instruction docs (CLAUDE.md / AGENTS.md) helpers.

AGENTS.md 是 Codex CLI 的指令文件。统一策略：有 CLAUDE.md 的仓库就该有
AGENTS.md（正常态 = 指向 CLAUDE.md 的 symlink，单一事实源）。两个注入时机：
- project 创建（clone/init）：backend/api/projects.py 注入并随 CLAUDE.md commit
- 任务启动（dispatcher）：惰性补齐存量项目——任何老项目下次跑任务时自动补上
"""
import logging
import os

logger = logging.getLogger(__name__)


def inject_agents_md(local_path: str) -> bool:
    """Create AGENTS.md (Codex CLI 的指令文件) pointing at CLAUDE.md.

    Symlink 保持单一事实源；不支持 symlink 的平台（如无特权 Windows）
    回退为一个指向 CLAUDE.md 的普通 pointer 文件。CLAUDE.md 不存在或
    AGENTS.md 已存在（含悬空链接）时不动。Returns True if created.
    """
    agents_path = os.path.join(local_path, "AGENTS.md")
    if os.path.lexists(agents_path):
        return False
    if not os.path.exists(os.path.join(local_path, "CLAUDE.md")):
        return False
    try:
        os.symlink("CLAUDE.md", agents_path)
    except OSError:
        with open(agents_path, "w") as f:
            f.write(
                "# AGENTS.md\n\n"
                "本项目的完整规范（含任务生命周期和 git 流程）在 [CLAUDE.md](./CLAUDE.md)，"
                "请先完整阅读它再开始工作。\n\n"
                "本文件与 CLAUDE.md 必须保持关键内容同步：如果你要往本文件写入独立内容，"
                "同样的关键意思必须同步进 CLAUDE.md（反之亦然）；"
                "在支持 symlink 的平台上，优先用 `ln -sf CLAUDE.md AGENTS.md` 恢复单一事实源。\n"
            )
    return True


def ensure_agents_md(local_path: str | None) -> bool:
    """Best-effort variant for hot paths (task launch): never raises.

    不 commit——留给 agent 的正常 git 流程带入（避免动到用户仓库的
    index/当前分支）；再次调用时已存在即 no-op，天然幂等。
    """
    if not local_path:
        return False
    try:
        created = inject_agents_md(local_path)
        if created:
            logger.info(f"Backfilled AGENTS.md into {local_path}")
        return created
    except Exception as e:
        logger.debug(f"ensure_agents_md({local_path}) failed: {e}")
        return False
