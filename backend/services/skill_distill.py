"""Skill Distill — analyze conversation history to propose new skills.

Reference: MiMo Code's /distill (6-phase evidence-based creation)
Simplified for CCM: analyze recent task patterns, propose skill candidates.

Trigger: manual ($distill command) or periodic (curator loop, every 30 days)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def analyze_patterns(db: AsyncSession, days: int = 30) -> dict:
    """Analyze recent task history for repeating patterns.

    MiMo's 6-phase approach (simplified):
      1. Locate data sources (log_entries)
      2. Inventory existing skills
      3. Discover repeated workflows from history
      4. Confirm against raw data
      5. Shortlist (occurred >= 2 times, stable inputs)
      6. Propose skill candidates
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    # Phase 1-2: Get recent tool usage patterns
    result = await db.execute(text("""
        SELECT tool_name, COUNT(*) as uses,
               COUNT(DISTINCT task_id) as unique_tasks,
               SUM(CASE WHEN is_error THEN 1 ELSE 0 END) as errors
        FROM log_entries
        WHERE tool_name IS NOT NULL
          AND timestamp > :cutoff
          AND event_type = 'tool_use'
        GROUP BY tool_name
        HAVING uses >= 3
        ORDER BY uses DESC
        LIMIT 20
    """), {"cutoff": cutoff})

    tool_patterns = [
        {
            "tool_name": row[0],
            "total_uses": row[1],
            "unique_tasks": row[2],
            "error_count": row[3],
            "error_rate": row[3] / row[1] if row[1] > 0 else 0,
        }
        for row in result.all()
    ]

    # Phase 3: Find frequently failing tools (potential skill candidates)
    high_error_tools = [p for p in tool_patterns if p["error_rate"] > 0.2 and p["error_count"] >= 2]

    # Phase 4: Get common error messages for high-error tools
    candidates = []
    for tool in high_error_tools:
        error_result = await db.execute(text("""
            SELECT content, COUNT(*) as occurrences
            FROM log_entries
            WHERE tool_name = :tool_name
              AND is_error = 1
              AND timestamp > :cutoff
              AND content IS NOT NULL
            GROUP BY content
            HAVING occurrences >= 2
            ORDER BY occurrences DESC
            LIMIT 3
        """), {"tool_name": tool["tool_name"], "cutoff": cutoff})

        common_errors = [
            {"error": row[0][:200], "count": row[1]}
            for row in error_result.all()
        ]

        if common_errors:
            candidates.append({
                "tool_name": tool["tool_name"],
                "total_uses": tool["total_uses"],
                "error_rate": round(tool["error_rate"] * 100, 1),
                "common_errors": common_errors,
                "suggestion": f"Create a skill with lessons about common {tool['tool_name']} errors",
            })

    # Phase 5-6: Also find frequently used tool combinations
    combo_result = await db.execute(text("""
        SELECT a.tool_name, b.tool_name, COUNT(*) as combo_count
        FROM log_entries a
        JOIN log_entries b ON a.task_id = b.task_id
          AND a.tool_name < b.tool_name
          AND a.event_type = 'tool_use'
          AND b.event_type = 'tool_use'
        WHERE a.timestamp > :cutoff
          AND a.tool_name IS NOT NULL
          AND b.tool_name IS NOT NULL
        GROUP BY a.tool_name, b.tool_name
        HAVING combo_count >= 5
        ORDER BY combo_count DESC
        LIMIT 10
    """), {"cutoff": cutoff})

    combos = [
        {
            "tools": [row[0], row[1]],
            "co_occurrence": row[2],
            "suggestion": f"These tools are frequently used together — a workflow skill could help",
        }
        for row in combo_result.all()
    ]

    return {
        "period_days": days,
        "tool_patterns": tool_patterns[:10],
        "skill_candidates": candidates,
        "tool_combos": combos,
        "summary": f"Analyzed {len(tool_patterns)} active tools. "
                   f"Found {len(candidates)} skill candidates from error patterns, "
                   f"{len(combos)} tool combinations.",
    }
