# 知识库系统设计：LLM Wiki 模式在 CCM 中的应用

> 参考来源：[Andrej Karpathy — LLM Wiki Pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
>
> 核心观点：Agent 在工程项目中犯的错误高度同质化，尤其在有重复模式的代码库中。通过构建**持久化的、结构化的本地知识库**，让 Agent 每次工作前先读、工作后回写，可以不断迭代形成每个用户/项目特有的知识体系。

---

## 1. 问题分析：为什么 Agent 会重复犯错

### 1.1 根本原因

Agent（Claude Code）每次启动时是"无状态"的——它没有跨 session 的记忆。虽然 CLAUDE.md 提供了项目级约定，但它是**手动维护的静态文档**，存在几个问题：

| 问题 | 表现 |
|------|------|
| **粒度太粗** | CLAUDE.md 是整个项目的概要，不可能详细到每个模块的陷阱 |
| **只记规范不记教训** | 约定了"该怎么做"，但没有记录"为什么不能那样做" |
| **无法检索** | Agent 每次都要读完全文，无法按当前任务精准查找相关经验 |
| **更新滞后** | 靠人记得去更新，实际上经常忘记 |

### 1.2 PROGRESS.md 的局限

CCM 已有 PROGRESS.md 记录经验教训，但它是**纯 append-only 的流水账**：
- 没有结构化标签，无法按问题类型检索
- 没有交叉引用，相关经验分散在不同条目中
- 没有分类——bug 修复、架构决策、性能优化混在一起
- Agent 不会主动去读它（dispatcher prompt 里没有引导）

### 1.3 Karpathy 的 LLM Wiki 模式如何解决

Karpathy 提出的三层架构：

```
Raw Sources（原始素材）→ Wiki（LLM 维护的结构化知识）→ Schema（规则和工作流定义）
```

核心洞察：**知识库的维护是记账工作（bookkeeping），LLM 天然擅长这个。** 人类负责判断和策展，LLM 负责整理、交叉引用、保持一致性。

---

## 2. CCM 现有机制的映射

在引入新功能之前，先梳理 CCM 已有的"proto-wiki"元素：

| Karpathy 三层 | CCM 现有对应 | 差距 |
|---------------|-------------|------|
| **Raw Sources** | 代码仓库、git history、PROGRESS.md | 有素材但未提炼 |
| **Wiki** | ❌ 不存在 | 缺少结构化的知识层 |
| **Schema** | CLAUDE.md + dispatcher prompt 模板 | 有规则但无知识检索流程 |

CCM 目前的知识注入链路：

```
CLAUDE.md（项目级约定）
    ↓
dispatcher._build_prompt()  →  "请阅读项目根目录的 CLAUDE.md 了解项目规范"
    ↓
Claude Code 读 CLAUDE.md → 开始工作
```

**缺失的环节**：Agent 在"读 CLAUDE.md"和"开始工作"之间，没有"查阅与当前任务相关的历史经验"这一步。

---

## 3. 设计方案

### 3.1 知识库的存储结构

每个 Project 拥有自己的知识库，存储在项目仓库中：

```
<project_root>/
├── CLAUDE.md                          # 不变：项目级约定（Schema 层）
├── .claude/
│   └── knowledge/                     # 新增：知识库（Wiki 层）
│       ├── index.md                   # 自动维护的索引
│       ├── anti-patterns/             # 反模式：踩过的坑
│       │   ├── async-session-leak.md
│       │   └── worktree-gitfile-dangling.md
│       ├── patterns/                  # 正向模式：验证过的做法
│       │   ├── alembic-migration-workflow.md
│       │   └── websocket-channel-naming.md
│       ├── conventions/               # 细粒度约定（CLAUDE.md 放不下的）
│       │   ├── sqlalchemy-relationship-loading.md
│       │   └── frontend-type-imports.md
│       └── decisions/                 # 架构决策记录（ADR）
│           ├── why-no-orm-relationships.md
│           └── why-keyword-only-args.md
```

每个知识条目格式：

```markdown
---
tags: [sqlalchemy, async, session]
severity: high          # 影响程度：low / medium / high / critical
discovered: 2026-05-20
commit: abc1234
related: [async-session-leak, worktree-gitfile-dangling]
---

# Worktree 的 .git 文件是指向主仓库的悬空指针

## 问题
rsync 部署时如果包含 worktree 的 `.git` 文件（它是一个指向主仓库 `.git/worktrees/` 的符号链接），
目标机器上的路径不存在会导致 git 命令全部失败。

## 解决方案
rsync 排除规则必须包含 `.git`，使用 `--filter ':- .gitignore'` 并额外 `--exclude .git`。

## 为什么不能用其他方式
- 不能只排除 `.git/`（目录），因为 worktree 的 .git 是文件
- 不能依赖 .gitignore，因为 .git 本身就不在 git 跟踪范围内
```

### 3.2 知识库的生命周期

```
        ┌──────────────────────────────────────────────────────────────┐
        │                     任务生命周期                              │
        │                                                              │
        │  ① 领取任务                                                   │
        │     ↓                                                        │
        │  ② 查阅知识库 ←── search_knowledge(task.title + description) │
        │     ↓                返回相关条目，注入 prompt                  │
        │  ③ 实现功能                                                   │
        │     ↓                                                        │
        │  ④ 遇到问题 → 解决                                           │
        │     ↓                                                        │
        │  ⑤ 提交代码                                                   │
        │     ↓                                                        │
        │  ⑥ 回写知识库 ←── add_knowledge() / update_knowledge()       │
        │     ↓                自动提取本次新发现的模式或陷阱             │
        │  ⑦ 标记完成                                                   │
        └──────────────────────────────────────────────────────────────┘
```

### 3.3 作为 MCP Skill 实现

利用 CCM 已有的 MCP Skills 系统，知识库可以作为一个新的 Skill 注入：

**Task.enabled_skills**: `{"knowledge_base": true, "monitor": false}`

**MCP 工具定义**（新增 `ccm_knowledge_server.py`）：

| 工具名 | 功能 | 调用时机 |
|--------|------|----------|
| `search_knowledge(query, tags?)` | 语义搜索知识库，返回相关条目 | 任务开始前、遇到问题时 |
| `add_knowledge(title, category, content, tags, severity?)` | 添加新条目 | 解决问题后、发现新模式后 |
| `update_knowledge(slug, content)` | 更新已有条目 | 发现更好的解决方案时 |
| `list_knowledge(category?, tags?)` | 列出知识条目 | 需要浏览已有知识时 |

**Dispatcher prompt 变化**：

```python
# 现有
parts = ["请阅读项目根目录的 CLAUDE.md 了解项目规范和任务完成后的 git 流程。"]

# 新增（当 knowledge_base skill 启用时）
parts.append("任务开始前，请先用 search_knowledge 工具查阅与本任务相关的历史经验和已知陷阱。")
parts.append("完成任务后，如果过程中遇到了值得记录的问题或发现了新的模式，请用 add_knowledge 写入知识库。")
```

### 3.4 知识库的自动维护（Lint）

借鉴 Karpathy 的 Lint 操作，可以通过 Monitor Skill 定期执行：

- **去重**：合并描述相同问题的条目
- **过期检查**：标记引用了已删除代码的条目
- **交叉引用**：发现条目间的关联但未 link 的情况
- **矛盾检测**：找出互相矛盾的建议

可作为一个定期 task 运行（mode="loop"），或者作为 Monitor 子 agent 在每次任务结束后触发。

---

## 4. 多层级知识体系

你朋友提到的"每个用户特有的知识体系"，可以分三个层级：

```
┌─────────────────────────────────────────────┐
│            全局知识库（Global）                │
│  跨项目通用的工程经验                          │
│  例：Alembic migration 的通用 best practice    │
│  存储位置：~/.claude-manager/knowledge/        │
├─────────────────────────────────────────────┤
│           项目知识库（Project）                │
│  特定项目的约定和陷阱                          │
│  例：CCM 的 keyword-only args 约定             │
│  存储位置：<project>/.claude/knowledge/        │
├─────────────────────────────────────────────┤
│           用户知识库（User）                   │
│  个人编码风格和偏好                            │
│  例：偏好函数式写法、不喜欢 class component     │
│  存储位置：~/.claude-manager/user-knowledge/   │
└─────────────────────────────────────────────┘
```

查询时自下而上合并：用户知识 > 项目知识 > 全局知识。冲突时以更具体的层级为准。

---

## 5. 与 CCM 现有组件的集成点

### 5.1 与 CLAUDE.md 的关系

CLAUDE.md 继续作为项目的**顶层约定文件**（Schema 层），知识库是它的**补充**而非替代。区别：

| CLAUDE.md | 知识库 |
|-----------|--------|
| 手动维护，人类审核 | Agent 自主维护，可选人类审核 |
| 高度浓缩的规则 | 详细的上下文和案例 |
| 全量读取 | 按需检索 |
| 项目级概要 | 模块级/问题级细节 |

### 5.2 与 PROGRESS.md 的关系

PROGRESS.md 继续作为**开发流水记录**，知识库从中**提炼**结构化知识：

```
PROGRESS.md（Raw Source）→ Agent 提炼 → 知识库条目（Wiki）
```

可以做一次性的初始导入：让 Agent 读 PROGRESS.md，提取其中的教训，生成知识库初始条目。

### 5.3 与 Monitor Skill 的关系

Monitor 子 agent 可以扩展为"知识库维护者"角色：
- 在主 agent 工作时，monitor 观察其行为
- 发现 agent 陷入已知坑时，主动提醒（通过 report_status）
- 任务结束后，自动提取值得记录的经验

### 5.4 与分布式 Worker 的关系

Worker 同步部署时（rsync），知识库随项目代码一起同步。全局知识库需要单独的同步机制（可通过 Worker bootstrap 阶段拉取）。

---

## 6. 实现路径

建议分三期实现：

### Phase 1：最小可用版本（MVP）

- 知识库目录结构 + Markdown 文件格式
- 新增 `ccm_knowledge_server.py`，实现 `search_knowledge` 和 `add_knowledge` 两个 MCP 工具
- 搜索用简单的文件名 + 标签匹配（不需要向量数据库）
- 在 dispatcher prompt 中加入查阅/回写引导语
- Task 创建表单添加 `knowledge_base` skill 开关

**预期效果**：Agent 在每次任务中能查阅已有知识、记录新发现，知识库逐步积累。

### Phase 2：智能检索 + 自动维护

- 语义搜索：根据任务描述自动匹配最相关的知识条目（可用 embedding 或 LLM 判断）
- 自动 lint：定期检查知识库一致性
- 知识库初始化：从 PROGRESS.md 一次性提取历史经验
- 多层级知识合并（全局 + 项目 + 用户）

### Phase 3：知识库的闭环验证

- 知识有效性追踪：某条知识被引用后，任务是否成功？
- 知识过期检测：引用的代码/API 已变更时自动标记
- 知识推荐：根据任务类型主动推送最相关的注意事项
- 跨项目知识迁移：从一个项目的经验泛化到其他项目

---

## 7. 类比：论文知识库 vs 工程知识库

你朋友已经用这个模式构建了论文阅读知识库。工程场景有几个不同点值得注意：

| 维度 | 论文知识库 | 工程知识库 |
|------|-----------|-----------|
| **素材来源** | 论文 PDF（静态） | 代码 + git history（动态演化） |
| **知识衰减** | 慢（论文不变） | 快（代码频繁重构，旧经验可能失效） |
| **验证方式** | 交叉引用文献 | 运行测试、看代码是否还存在 |
| **写入时机** | 批量导入 | 实时写入（每次任务结束） |
| **矛盾处理** | 保留多视角 | 以最新为准（但保留历史理由） |

工程知识库的关键挑战是**知识衰减**——一个月前的"最佳实践"可能因为代码重构已经不适用了。这就是为什么需要 lint 机制和代码关联检查。

---

## 8. 总结

Karpathy 的 LLM Wiki 模式对 CCM 的启发：

1. **Agent 的错误是可预防的**——同质化的坑只需要踩一次，前提是有机制把教训传递给下一次执行
2. **CLAUDE.md 是必要但不充分的**——它是 Schema 层，还需要 Wiki 层来存储细粒度的、可检索的经验知识
3. **知识的维护应该由 Agent 自己做**——人类负责方向和策展，Agent 负责记账和整理
4. **CCM 的 MCP Skills 系统天然适合承载这个功能**——不需要大的架构改动，新增一个 Skill 即可

最终目标：**每个项目随着使用时间的增长，积累起一套越来越准确的"该做什么、不该做什么"的知识体系，让每个新任务都能站在所有历史经验的肩膀上。**
