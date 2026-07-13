"""Inject user-created skills into agent prompt."""

from __future__ import annotations

import os
import sqlite3
import tempfile

from backend.models.user_skill import UserSkill


def build_user_skill_prompt_sync(task_id: int) -> str | None:
    """Build a prompt file with user skill L0 directory (sync, for _build_command).

    Returns path to temp file, or None if no user skills selected.
    """
    from backend.config import settings
    db_url = settings.database_url
    if "sqlite" not in db_url:
        return None
    raw = db_url.split("///", 1)[-1] if "///" in db_url else db_url
    conn = sqlite3.connect(raw)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT selected_user_skills FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row or not row["selected_user_skills"]:
            return None
        import json
        skill_ids = json.loads(row["selected_user_skills"])
        if not skill_ids:
            return None
        placeholders = ",".join("?" * len(skill_ids))
        skills = conn.execute(
            f"SELECT id, name, description FROM user_skills WHERE id IN ({placeholders})",
            skill_ids,
        ).fetchall()
    except sqlite3.Error:
        # Fail-open：注入用户技能是增强项，DB 文件缺失/表不存在（全新部署、
        # 测试 worktree）绝不能炸掉 launch 本身
        return None
    finally:
        conn.close()

    if not skills:
        return None

    lines = [
        "## User Skills\n",
        "The following user-defined skills are available for this task.",
        "Use the MCP tool ccm_read_user_skill(id) to load full content when needed.\n",
    ]
    for s in skills:
        desc = (s["description"] or "").strip().replace("\n", " ")[:100]
        lines.append(f"- **{s['name']}** (id={s['id']}): {desc}")
    lines.append("")

    content = "\n".join(lines)
    fd, path = tempfile.mkstemp(prefix=f"ccm-user-skills-{task_id}-", suffix=".md")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
