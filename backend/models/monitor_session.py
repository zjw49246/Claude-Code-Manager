"""Compatibility shim — monitor 表已通用化为 sub-agent 表。

真正的模型在 backend.models.sub_agent：
- MonitorSession → SubAgentSession（表 sub_agent_sessions，加 agent_type/source/meta）
- MonitorCheck   → SubAgentReport（表 sub_agent_reports，monitor_session_id → session_id，
  旧属性名经 synonym 兼容）

新代码请直接 import SubAgentSession / SubAgentReport。
"""

from backend.models.sub_agent import (  # noqa: F401
    SubAgentSession as MonitorSession,
    SubAgentReport as MonitorCheck,
)
