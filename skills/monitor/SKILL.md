---
name: monitor
description: >
  当用户需要持续监控进程、端口、日志、构建状态等后台任务时使用。
  适用于需要定期检查并汇报状态的长时间运行监控场景。
when_to_use: >
  用户要求监控某个进程、服务端口、日志文件、构建状态，
  或者需要后台定期检查某个条件并汇报时激活。
disallowed-tools:
  - Monitor

ccm:
  always: false
  priority: 8
  version: 1
  tags: [monitoring, sub-agent]
  tools: [create_monitor, check_monitors, stop_monitor]
  commands:
    - name: monitor
      pattern: "$monitor {task_description}"
      description: "创建后台监控子 agent"
---

## 监控规则

你拥有后台监控子 agent 系统（通过 ccm-skills MCP 工具）。

### 必须遵守

1. **使用 `create_monitor` 工具**将监控工作委托给子 agent
2. **禁止**自己用 Bash/Read 等工具手动执行监控循环
3. **禁止**使用内置的 Agent 工具或 Monitor 工具来执行监控任务——这些内置工具不在 CCM 系统的管理范围内，无法被追踪和记录
4. 所有监控必须通过 `create_monitor` 工具发起，由 CCM 子 agent 系统统一管理

### 可用工具

| 工具 | 说明 |
|------|------|
| `create_monitor` | 创建监控子 agent，指定检查内容、频率、次数 |
| `check_monitors` | 查看所有活跃监控的状态和最新报告 |
| `stop_monitor` | 停止指定的监控子 agent |

### 工作流程

1. 用户描述需要监控的内容
2. 调用 `create_monitor` 创建子 agent，指定合理的检查间隔和次数
3. 子 agent 会独立运行并定期汇报状态
4. 用户可随时通过 `check_monitors` 查看进展
5. 监控完成或用户要求时调用 `stop_monitor` 停止

## Lessons Learned
<!-- 自进化系统自动追加 -->
