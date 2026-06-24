---
name: skill-management
description: >
  管理和创建技能（Skill）。当需要创建新技能、分析使用模式、或启用/禁用技能时使用。
when_to_use: >
  用户要求创建新 skill、分析技能使用情况、或调整当前 task 的技能配置时激活。

ccm:
  always: false
  priority: 5
  version: 1
  tags: [skill, management]
  tools: [ccm_create_skill, ccm_enable_skill, ccm_disable_skill, ccm_distill, ccm_read_skill]
  commands:
    - name: distill
      pattern: "$distill"
      description: "分析近期使用模式，提炼新技能建议"
---

## 技能管理

你可以通过 MCP 工具管理 CCM 的技能系统。

### 可用工具

| 工具 | 说明 |
|------|------|
| `ccm_read_skill` | 读取指定技能的完整内容和历史教训 |
| `ccm_create_skill` | 创建新的 SKILL.md 文件 |
| `ccm_enable_skill` | 为当前 task 启用指定技能 |
| `ccm_disable_skill` | 为当前 task 禁用指定技能 |
| `ccm_distill` | 分析近期工具使用模式，提炼新技能建议 |

### 创建新技能的规范

1. **name**：简短的 kebab-case 标识符（如 `code-review`）
2. **description**：一句话描述何时使用（不超过 150 字）
3. **body**：具体的规则和工作流程，面向 agent 编写
4. **tags**：分类标签，用于进化系统定位关联技能
5. **tools**：关联的 MCP 工具名，用于失败反思时匹配

### 注意事项

- 技能文件存储在 `skills/{name}/SKILL.md`
- 技能的教训（lessons）存储在数据库中，不写入文件
- 通过 `ccm_read_skill` 读取时会自动合并数据库中的教训

## Lessons Learned
<!-- 自进化系统自动追加 -->
