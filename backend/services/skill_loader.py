"""Skill discovery and injection for CCM.

Skills are SKILL.md files (Agent Skills standard) stored in:
  1. {CCM_REPO}/skills/         — global skills (deployed with code)
  2. {project}/.ccm/skills/     — project-level overrides

Injection:
  - always:true skills → body injected into --append-system-prompt-file
  - All skills → name+description listed as L0 directory
  - On-demand → MCP tool ccm_read_skill returns full body + DB lessons
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Budget defaults (from agent-ml-research)
MAX_ALWAYS_PROMPT_CHARS = 4000
MAX_ALWAYS_IN_PROMPT = 10
MAX_L0_DESCRIPTION_CHARS = 1536


@dataclass
class SkillCCM:
    """CCM-specific extension fields from SKILL.md frontmatter."""
    always: bool = False
    priority: int = 0
    version: int = 1
    tags: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    heavy: bool = False


@dataclass
class Skill:
    """Parsed SKILL.md."""
    name: str
    description: str = ""
    when_to_use: str = ""
    body: str = ""
    path: Path | None = None
    scope: str = "global"
    disallowed_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    ccm: SkillCCM = field(default_factory=SkillCCM)


def parse_skill(skill_md_path: Path) -> Skill | None:
    """Parse a SKILL.md file into a Skill object."""
    try:
        content = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Cannot read skill file: %s", skill_md_path)
        return None

    # Split YAML frontmatter and body
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not match:
        logger.warning("No YAML frontmatter in %s", skill_md_path)
        return None

    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML in %s: %s", skill_md_path, e)
        return None

    body = match.group(2).strip()

    # Parse CCM extension
    ccm_raw = fm.get("ccm", {}) or {}
    ccm = SkillCCM(
        always=ccm_raw.get("always", False),
        priority=ccm_raw.get("priority", 0),
        version=ccm_raw.get("version", 1),
        tags=ccm_raw.get("tags", []) or [],
        tools=ccm_raw.get("tools", []) or [],
        roles=ccm_raw.get("roles", []) or [],
        modes=ccm_raw.get("modes", []) or [],
        commands=ccm_raw.get("commands", []) or [],
        triggers=ccm_raw.get("triggers", []) or [],
        heavy=len(body) > 5000,
    )

    return Skill(
        name=fm.get("name", skill_md_path.parent.name),
        description=fm.get("description", "")[:MAX_L0_DESCRIPTION_CHARS],
        when_to_use=fm.get("when_to_use", ""),
        body=body,
        path=skill_md_path,
        scope="global",
        disallowed_tools=fm.get("disallowed-tools", []) or [],
        allowed_tools=fm.get("allowed-tools", []) or [],
        ccm=ccm,
    )


def discover_skills(
    ccm_repo_dir: str | Path | None = None,
    project_dir: str | Path | None = None,
    role: str | None = None,
    mode: str | None = None,
    exclude: set[str] | None = None,
) -> dict[str, Skill]:
    """Discover all available skills.

    Scans:
      1. {ccm_repo}/skills/       — global
      2. {project}/.ccm/skills/   — project-level (overrides same-name global)

    Filters by role, mode. Excludes names in `exclude` set.
    """
    skills: dict[str, Skill] = {}

    # 1. Global skills from CCM repo
    if ccm_repo_dir is None:
        ccm_repo_dir = Path(__file__).resolve().parents[2]
    repo_skills = Path(ccm_repo_dir) / "skills"
    if repo_skills.is_dir():
        for skill_dir in sorted(repo_skills.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if skill_md.is_file():
                skill = parse_skill(skill_md)
                if skill:
                    skill.scope = "global"
                    skills[skill.name] = skill

    # 2. Project-level skills (override same-name)
    if project_dir:
        proj_skills = Path(project_dir) / ".ccm" / "skills"
        if proj_skills.is_dir():
            for skill_dir in sorted(proj_skills.iterdir()):
                skill_md = skill_dir / "SKILL.md"
                if skill_md.is_file():
                    skill = parse_skill(skill_md)
                    if skill:
                        skill.scope = "project"
                        skills[skill.name] = skill

    # Filter by role
    if role:
        skills = {
            k: v for k, v in skills.items()
            if not v.ccm.roles or role in v.ccm.roles
        }

    # Filter by mode
    if mode:
        skills = {
            k: v for k, v in skills.items()
            if not v.ccm.modes or mode in v.ccm.modes
        }

    # Exclude
    if exclude:
        skills = {k: v for k, v in skills.items() if k not in exclude}

    logger.info("Discovered %d skills", len(skills))
    return skills


def select_always_skills(
    skills: dict[str, Skill],
    max_count: int = MAX_ALWAYS_IN_PROMPT,
    max_chars: int = MAX_ALWAYS_PROMPT_CHARS,
) -> list[Skill]:
    """Select always-on skills within token budget.

    Greedy by priority descending. First skill always included even if
    it exceeds max_chars (agent-ml-research design).
    """
    always = [s for s in skills.values() if s.ccm.always]
    sorted_skills = sorted(always, key=lambda s: s.ccm.priority, reverse=True)

    selected: list[Skill] = []
    total_chars = 0
    for skill in sorted_skills:
        body_len = len(skill.body)
        if len(selected) >= max_count:
            break
        if total_chars + body_len > max_chars and selected:
            break
        selected.append(skill)
        total_chars += body_len
    return selected


def build_skill_prompt_file(
    skills: dict[str, Skill],
    enabled_skills: dict[str, bool] | None = None,
    task_id: int | None = None,
) -> str:
    """Build a system prompt file with skill directory + always-on bodies.

    Returns the path to the generated temp file.
    """
    import tempfile

    lines: list[str] = []

    # Filter to enabled skills (if enabled_skills provided)
    active = skills
    if enabled_skills is not None:
        active = {
            k: v for k, v in skills.items()
            if enabled_skills.get(k, False) or v.ccm.always
        }

    # L0: Skill directory (all active skills, name + description)
    if active:
        lines.append("## Available Skills\n")
        lines.append("The following skills are available. Use ccm_read_skill(name) to load full details.\n")
        for skill in sorted(active.values(), key=lambda s: s.ccm.priority, reverse=True):
            desc = skill.description.strip().replace("\n", " ")[:100]
            lines.append(f"- **{skill.name}**: {desc}")
        lines.append("")

    # L1: Always-on skill bodies (budget-controlled)
    always_skills = select_always_skills(active)
    if always_skills:
        lines.append("## Active Skill Instructions\n")
        for skill in always_skills:
            lines.append(f"### {skill.name}\n")
            lines.append(skill.body)
            lines.append("")

    content = "\n".join(lines)
    if not content.strip():
        return ""

    # Write to temp file
    suffix = f"-{task_id}" if task_id else ""
    fd, path = tempfile.mkstemp(prefix=f"ccm-skills{suffix}-", suffix=".md")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return path


def get_skill_disallowed_tools(
    skills: dict[str, Skill],
    enabled_skills: dict[str, bool] | None = None,
) -> list[str]:
    """Collect disallowed-tools from all enabled skills."""
    disallowed: set[str] = set()
    for name, skill in skills.items():
        if enabled_skills and not enabled_skills.get(name, False):
            continue
        disallowed.update(skill.disallowed_tools)
    return sorted(disallowed)
