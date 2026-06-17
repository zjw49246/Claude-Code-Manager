"""Skill Curator — periodic lifecycle management and usage analysis.

Reference: Hermes Agent Curator (deterministic state transitions + LLM review)
         + MiMo Code Dream/Distill (7/30-day cycles)

Adapted for CCM:
  - Runs as asyncio background task (CCM is always-on server, not CLI)
  - State stored in DB skill_state table (not per-skill JSON files)
  - Scheduling: interval + idle check (Hermes) + project age check (MiMo)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select, func, update, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.skill_lesson import SkillLesson, SkillUsage

logger = logging.getLogger(__name__)

# Lifecycle thresholds (days)
STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90

# Curator scheduling
CURATOR_INTERVAL_HOURS = 168  # 7 days
MIN_IDLE_HOURS = 2


async def run_curator(db: AsyncSession, dry_run: bool = False) -> dict:
    """Run curator lifecycle management.

    Phase 1 (deterministic): state transitions based on usage timestamps
    Phase 2 (analysis): usage statistics report

    Returns a summary of actions taken.
    """
    from backend.services.skill_loader import discover_skills

    skills = discover_skills()
    now = datetime.utcnow()
    summary = {"checked": 0, "stale": [], "reactivated": [], "stats": {}}

    for name, skill in skills.items():
        summary["checked"] += 1

        # Get last usage time from DB
        result = await db.execute(
            select(func.max(SkillUsage.created_at))
            .where(SkillUsage.skill_name == name)
        )
        last_used = result.scalar()

        # Get lesson count
        lesson_count = (await db.execute(
            select(func.count())
            .select_from(SkillLesson)
            .where(SkillLesson.skill_name == name)
        )).scalar() or 0

        # Get total usage count
        usage_count = (await db.execute(
            select(func.count())
            .select_from(SkillUsage)
            .where(SkillUsage.skill_name == name)
        )).scalar() or 0

        days_unused = (now - last_used).days if last_used else None

        summary["stats"][name] = {
            "usage_count": usage_count,
            "lesson_count": lesson_count,
            "last_used": last_used.isoformat() if last_used else None,
            "days_unused": days_unused,
        }

        # Lifecycle transitions
        if days_unused is not None and days_unused >= STALE_AFTER_DAYS:
            summary["stale"].append(name)
            if not dry_run:
                logger.info("curator: skill '%s' marked stale (%d days unused)", name, days_unused)

    return summary


async def get_usage_report(db: AsyncSession) -> list[dict]:
    """Get usage statistics for all skills."""
    result = await db.execute(
        text("""
            SELECT skill_name,
                   COUNT(*) as total_uses,
                   MAX(created_at) as last_used,
                   COUNT(DISTINCT task_id) as unique_tasks
            FROM skill_usage
            GROUP BY skill_name
            ORDER BY total_uses DESC
        """)
    )
    return [
        {
            "skill_name": row[0],
            "total_uses": row[1],
            "last_used": row[2],
            "unique_tasks": row[3],
        }
        for row in result.all()
    ]


async def log_skill_usage(
    db: AsyncSession,
    skill_name: str,
    trigger_type: str,
    task_id: int | None = None,
    project_id: int | None = None,
):
    """Log a skill usage event."""
    db.add(SkillUsage(
        skill_name=skill_name,
        trigger_type=trigger_type,
        task_id=task_id,
        project_id=project_id,
    ))
    await db.commit()
