"""Skill evolution — learn from tool failures.

When a tool execution fails, the system:
1. Finds the related skill via tools → tags → name matching
2. Reflects on the failure with a lightweight LLM
3. Deduplicates against existing lessons (character bigram overlap)
4. Stores the lesson in DB (not in skill files — Worker sync safe)

Reference: agent-ml-research's evolution.py (3-tier matching, 600s cooldown,
60% word overlap dedup). Adapted for CCM: DB storage instead of file writes,
bigram dedup for Chinese compatibility.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.skill_lesson import SkillLesson

logger = logging.getLogger(__name__)

# Cooldown: same skill can only evolve once per 600 seconds
_COOLDOWN_SECONDS = 600

# In-memory cooldown tracker (simple for single-process; DB-backed for multi-process)
_cooldowns: dict[str, float] = {}


def _bigram_set(text: str) -> set[str]:
    """Character-level bigrams, works for both Chinese and English."""
    text = re.sub(r"\s+", "", text.lower())
    return {text[i : i + 2] for i in range(len(text) - 1)}


def is_duplicate(new_lesson: str, existing_lessons: list[str], threshold: float = 0.5) -> bool:
    """Check if new_lesson is a duplicate of any existing lesson.

    Uses character bigram overlap (not word overlap) for Chinese compatibility.
    """
    new_bg = _bigram_set(new_lesson)
    if not new_bg:
        return False
    for old in existing_lessons:
        old_bg = _bigram_set(old)
        if not old_bg:
            continue
        overlap = len(new_bg & old_bg) / min(len(new_bg), len(old_bg))
        if overlap > threshold:
            return True
    return False


def find_related_skill(tool_name: str, skills: dict) -> str | None:
    """Find the skill most related to a tool failure.

    3-tier matching (from agent-ml-research):
      1. Explicit tools list in skill frontmatter
      2. Tag match
      3. Name substring match
    """
    # Level 1: explicit tools list
    for name, skill in skills.items():
        if tool_name in (skill.ccm.tools or []):
            return name

    # Level 2: tag match
    tool_lower = tool_name.lower()
    for name, skill in skills.items():
        if any(tool_lower in tag.lower() for tag in (skill.ccm.tags or [])):
            return name

    # Level 3: name substring
    for name, skill in skills.items():
        if tool_lower in name.lower() or name.lower() in tool_lower:
            return name

    return None


async def evolve_on_failure(
    tool_name: str,
    error: str,
    context: str,
    db: AsyncSession,
    skills: dict | None = None,
    worker_id: int | None = None,
) -> bool:
    """Learn from a tool failure. Returns True if a lesson was added."""

    # 1. Discover skills if not provided
    if skills is None:
        from backend.services.skill_loader import discover_skills
        skills = discover_skills()

    # 2. Find related skill
    related = find_related_skill(tool_name, skills)
    if not related:
        logger.debug("evolution: no related skill for tool %s", tool_name)
        return False

    # 3. Cooldown check (in-memory)
    now = time.time()
    if _cooldowns.get(related, 0) > now - _COOLDOWN_SECONDS:
        logger.debug("evolution: skill %s in cooldown", related)
        return False

    # 4. Get existing lessons for dedup
    result = await db.execute(
        select(SkillLesson.lesson)
        .where(SkillLesson.skill_name == related)
        .order_by(SkillLesson.created_at.desc())
        .limit(5)
    )
    existing = [row[0] for row in result.all()]

    # 5. Reflect with lightweight LLM
    lesson = await _reflect_on_failure(tool_name, error, context, existing)
    if not lesson or lesson.strip().upper() == "SKIP":
        return False

    # 6. Dedup
    if is_duplicate(lesson, existing):
        logger.debug("evolution: duplicate lesson for skill %s", related)
        return False

    # 7. Store in DB
    lesson_hash = hashlib.md5(f"{related}:{lesson}".encode()).hexdigest()
    try:
        db.add(SkillLesson(
            skill_name=related,
            lesson=lesson,
            source="evolution",
            tool_name=tool_name,
            worker_id=worker_id,
            lesson_hash=lesson_hash,
        ))
        await db.commit()
    except Exception:
        await db.rollback()
        logger.debug("evolution: lesson already exists (hash collision)")
        return False

    # 8. Update cooldown
    _cooldowns[related] = now

    logger.info("evolution: learned lesson for skill %s from tool %s failure", related, tool_name)
    return True


async def get_lessons_for_skill(skill_name: str, db: AsyncSession, limit: int = 10) -> list[str]:
    """Get recent lessons for a skill (used when building skill prompt)."""
    result = await db.execute(
        select(SkillLesson.lesson, SkillLesson.created_at)
        .where(SkillLesson.skill_name == skill_name)
        .order_by(SkillLesson.created_at.desc())
        .limit(limit)
    )
    return [f"[{row[1].strftime('%Y-%m-%d')}] {row[0]}" for row in result.all()]


async def _reflect_on_failure(
    tool_name: str,
    error: str,
    context: str,
    existing_lessons: list[str],
) -> str | None:
    """Use a lightweight LLM to reflect on a failure and extract a lesson.

    Returns the lesson string, or "SKIP" for transient errors.
    """
    try:
        import anthropic

        client = anthropic.Anthropic()
        existing_text = "\n".join(f"- {l}" for l in existing_lessons[:5]) if existing_lessons else "（无）"

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"工具 `{tool_name}` 执行失败。\n"
                    f"错误信息：{error[:500]}\n"
                    f"上下文：{context[:300]}\n\n"
                    f"已有教训：\n{existing_text}\n\n"
                    "请提取一条简短的经验教训（一句话，中文）。"
                    "如果是网络超时等瞬时错误，或者和已有教训重复，输出 SKIP。"
                ),
            }],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.warning("evolution: LLM reflection failed: %s", e)
        return None
