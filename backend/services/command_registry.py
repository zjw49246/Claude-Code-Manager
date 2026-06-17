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


# ---------------------------------------------------------------------------
# Built-in commands
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
    name="monitor",
    description="创建后台监控子 agent，监控进程、端口、日志等",
    prompt_template=(
        "【重要 — 监控规则】你拥有后台监控子 agent 系统（通过 ccm-skills MCP 工具）。\n"
        "用户通过 $monitor 命令要求你执行监控任务。\n"
        "你必须调用 create_monitor 工具将监控工作委托给子 agent，"
        "禁止自己用 Bash/Read 等工具手动执行监控循环。\n"
        "【禁止】不要使用内置的 Agent 工具或 Monitor 工具来执行监控任务。"
        "这些内置工具不在 CCM 系统的管理范围内，无法被追踪和记录。"
        "所有监控必须通过 create_monitor 工具发起，由 CCM 子 agent 系统统一管理。\n"
        "子 agent 会独立运行并定期汇报状态，你通过 check_monitors 查看进展。\n"
        "可用工具: create_monitor（创建监控子agent）/ check_monitors（查看状态）/ stop_monitor（停止监控）。"
    ),
    required_skills={"monitor": True},
    # 只禁内置 Monitor（监控必须走 $monitor / CCM 子 agent 体系）；
    # Agent/Task 保持可用——原生子 agent 由 PTY 层观测并镜像进
    # sub_agent_sessions（native-agent），不需要一刀切禁掉
    disallowed_builtins=["Monitor"],
))

