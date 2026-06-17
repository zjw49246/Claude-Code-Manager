"""Command registry — maps $command names to skill configurations."""
from dataclasses import dataclass, field


@dataclass
class Command:
    name: str
    description: str
    prompt_template: str
    required_skills: dict[str, bool] = field(default_factory=dict)
    disallowed_builtins: list[str] = field(default_factory=list)
    always_available: bool = False


COMMAND_REGISTRY: dict[str, Command] = {}


def register_command(cmd: Command):
    COMMAND_REGISTRY[cmd.name] = cmd


def parse_command(message: str) -> tuple[Command | None, str]:
    """Parse $command from message. Returns (command, remaining_args) or (None, original_message)."""
    stripped = message.strip()
    if not stripped.startswith("$"):
        return None, message
    parts = stripped.split(None, 1)
    cmd_name = parts[0][1:]
    args = parts[1] if len(parts) > 1 else ""
    cmd = COMMAND_REGISTRY.get(cmd_name)
    if not cmd:
        return None, message
    return cmd, args


def get_default_skills() -> dict[str, bool]:
    """Return skills that should be enabled on every task by default.

    Now sources defaults from SKILL.md files (always:true) instead of
    the command registry's always_available flag.
    """
    try:
        from backend.services.skill_loader import discover_skills
        skills = discover_skills()
        return {name: True for name, skill in skills.items() if skill.ccm.always}
    except Exception:
        return {}


def ensure_default_skills(skills: dict | None) -> dict:
    """Merge default skills into the given skills dict."""
    result = dict(skills or {})
    for k, v in get_default_skills().items():
        result.setdefault(k, v)
    return result


def register_skill_commands():
    """Auto-register $commands from SKILL.md ccm.commands definitions.

    Skips commands already registered (built-in commands take precedence).
    Called once at import time; new skills are picked up on next process restart.
    """
    try:
        from backend.services.skill_loader import discover_skills
        skills = discover_skills()
    except Exception:
        return
    for skill_name, skill in skills.items():
        for cmd_def in skill.ccm.commands:
            cmd_name = cmd_def.get("name", "")
            if not cmd_name or cmd_name in COMMAND_REGISTRY:
                continue
            prompt = (
                f"用户通过 ${cmd_name} 命令触发了技能 '{skill_name}'。\n"
                f"请先调用 ccm_read_skill('{skill_name}') 获取完整技能指南，"
                "然后严格按照指南执行。"
            )
            register_command(Command(
                name=cmd_name,
                description=cmd_def.get("description", skill.description[:80]),
                prompt_template=prompt,
                required_skills={skill_name: True},
                disallowed_builtins=skill.disallowed_tools,
            ))


# ---------------------------------------------------------------------------
# Built-in commands (registered first, take precedence over skill commands)
# ---------------------------------------------------------------------------

register_command(Command(
    name="help",
    description="列出所有可用命令和工具，标注当前 task 已启用哪些",
    prompt_template=(
        "用户请求查看命令帮助。请调用 ccm_command_help 工具获取所有可用命令列表，"
        "然后向用户展示结果。格式清晰，标注哪些命令当前 task 已启用、哪些需要通过 $command 临时使用。"
    ),
    always_available=True,
))

register_command(Command(
    name="distill",
    description="分析近期使用模式，提炼新技能建议",
    prompt_template=(
        "用户请求分析技能提炼。请按以下步骤操作：\n"
        "1. 调用 ccm_distill 工具分析近期使用模式\n"
        "2. 解读分析结果，重点关注：\n"
        "   - 高错误率的工具 → 建议创建对应 skill\n"
        "   - 常一起使用的工具组合 → 建议创建工作流 skill\n"
        "3. 向用户展示结果，如果有好的建议，问用户是否要创建（可以用 ccm_create_skill 工具）\n"
    ),
    always_available=True,
))

# Auto-register commands from SKILL.md files (e.g. $monitor from monitor skill)
register_skill_commands()

