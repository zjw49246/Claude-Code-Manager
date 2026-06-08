# Agent-Based Testing 前沿调研报告 (2025–2026)

> 调研时间：2026-06-08
> 数据来源：学术论文（arXiv / ICSE / IEEE）、厂商技术博客、GitHub 开源项目、行业分析报告
> 验证方式：多源交叉验证 + 对抗性事实核查

---

## 目录

1. [行业全景：从脚本维护到意图驱动](#1-行业全景从脚本维护到意图驱动)
2. [商业平台横评](#2-商业平台横评)
   - 2.1 [Agentic QA 平台](#21-agentic-qa-平台)
   - 2.2 [浏览器驱动的 AI 测试工具](#22-浏览器驱动的-ai-测试工具)
   - 2.3 [代码优先 / 流量录制平台](#23-代码优先--流量录制平台)
3. [开源工具与框架](#3-开源工具与框架)
4. [Self-Healing 测试：不只是 Selector 修复](#4-self-healing-测试不只是-selector-修复)
5. [CI/CD 集成：Agent 进入流水线](#5-cicd-集成agent-进入流水线)
6. [学术研究前沿](#6-学术研究前沿)
   - 6.1 [LLM 测试生成的系统性综述](#61-llm-测试生成的系统性综述)
   - 6.2 [Agent 生成的测试真的有用吗？](#62-agent-生成的测试真的有用吗)
   - 6.3 [Agentic Property-Based Testing](#63-agentic-property-based-testing)
   - 6.4 [工业案例：人机协作测试](#64-工业案例人机协作测试)
   - 6.5 [SWE-Bench 与 Agent 能力评估](#65-swe-bench-与-agent-能力评估)
7. [人机协作：解决 Overlap 问题](#7-人机协作解决-overlap-问题)
8. [关键限制与未解决问题](#8-关键限制与未解决问题)
9. [对我们项目的启示](#9-对我们项目的启示)

---

## 1. 行业全景：从脚本维护到意图驱动

2026 年 AI 测试领域最清晰的趋势：**最快的团队已经不再维护测试脚本，而是转向描述测试目标（intent-driven testing）**。

Forrester 在 2025 Q4 将其测试分类从"Continuous Automation Testing Platforms"重命名为 **"Autonomous Testing Platforms"**，标志着行业对 Agentic QA 作为独立品类的正式认可。

**四种架构范式**（2026 年格局）：

| 架构类型 | 代表产品 | 核心思路 |
|---------|---------|---------|
| 低代码录制 + Selector 自愈 | Mabl, Testim (Tricentis) | 录制操作，AI 修复断裂的 selector |
| 自然语言规约执行 | Momentic, testRigor | 用自然语言描述测试，Agent 解释执行 |
| 运行时探索型 Agent | QA.tech | 视觉交互模型，构建应用知识图谱，主动探索边界用例 |
| 代码优先 + PR 感知 | Autonoma, Shiplight | 从源代码/PR diff 推导测试计划，每次 PR 自动重新生成 |

采用 Agentic QA 平台的团队通常在**相同 QA 人员编制下**实现 **5–10x 测试覆盖率增长**。传统 QA 团队将 **30–40% 的精力**花在测试维护上，AI 自愈能力可以大幅削减这一开销。

---

## 2. 商业平台横评

### 2.1 Agentic QA 平台

#### QA.tech
- **工作方式**：使用视觉交互模型（非 DOM 依赖），构建应用的**知识图谱**，主动进行探索性测试和边界用例发现
- **关键优势**：DOM 无关 → UI 重构不影响测试；~5 分钟创建一个测试；主动发现边界用例
- **成熟度**：生产可用
- **局限**：作为测评文章发布方，其对比数据可能存在偏向性
- **定价**：SaaS，按用量计费

#### Mabl
- **工作方式**：AI 原生 Agentic 测试器，可从用户故事自动构建端到端测试
- **关键优势**：无限并行执行；覆盖 Web 浏览器、原生/混合移动端、API、性能测试；自愈能力
- **成熟度**：生产就绪，已规模运营多年
- **定价**：商业 SaaS

#### testRigor
- **工作方式**：自然语言描述测试场景，平台解释并执行
- **关键优势**：非技术人员可以编写测试；跨平台（Web/移动端/API）；自愈
- **成熟度**：生产就绪，已规模运营

#### QA Wolf
- **工作方式**：诊断优先（diagnosis-first）的全类型自愈，声称解决几乎 100% 的 flaky test
- **关键优势**：覆盖 6 种自愈类型（selector / timing / runtime error / test data / visual assertion / interaction）；误报率 < 5%
- **成熟度**：生产就绪
- **差异化**：大多数工具只做 selector healing，而 QA Wolf 的诊断覆盖全部 6 种失败类型

#### Shiplight AI
- **工作方式**：Intent caching（缓存测试意图而非 DOM selector），使测试在 UI 重构后仍然有效
- **关键优势**：唯一声称集成了 **Model Context Protocol (MCP)** 的平台，使 AI 编码 Agent（Claude Code、Cursor、Codex）可以验证自己的工作
- **成熟度**：较新入场者
- **差异化**：MCP 集成 → AI 编码 Agent 可以直接调用测试验证

#### BrowserStack
- **工作方式**：5 个命名 AI Agent：Test Case Generator / Low-Code Authoring / Self-Healing / Visual Review / A11y Issue Detection
- **关键优势**：30,000+ iOS/Android 真实设备；成熟基础设施
- **成熟度**：生产就绪
- **定价**：Team Ultimate 起步 $225/月

#### Sauce Labs
- **工作方式**：AI 驱动的测试编写，声称 10x 速度提升
- **成熟度**：生产就绪

#### Katalon
- **工作方式**：TrueTest 通过行为分析实现自主测试生成
- **关键优势**：2025 Gartner 魔力象限 Visionary
- **成熟度**：生产就绪

### 2.2 浏览器驱动的 AI 测试工具

#### Playwright AI Agents (Microsoft)
- **工作方式**：Playwright MCP Server 暴露 20+ 浏览器控制工具，两种模式：
  - **Snapshot 模式**（默认）：使用 YAML 可访问性树，比截图快数量级
  - **Vision 模式**（回退）：使用截图
- **三个专用 Agent**：
  - **Planner**：探索应用
  - **Generator**：从 markdown 测试计划生成 TypeScript 代码（使用 `getByRole()` 定位器）
  - **Healer**：修复断裂测试，selector 相关失败修复成功率 > 75%
- **成熟度**：v1.56+，需要 VS Code v1.105+
- **定价**：开源免费

#### Stagehand (Browserbase)
- **工作方式**：自愈测试 + 自动缓存。首次解析 selector 后缓存，UI 变化时 cache miss 触发 AI 重新定位
- **成功率**：新任务约 75%
- **风险**：模型版本升级可能导致不兼容（已有 model upgrade 导致 `act()` 调用失败的案例）

#### Agent-Browser (Vercel Labs)
- **工作方式**：发送可访问性树快照而非完整 DOM/截图，声称 snapshot 体积比 Playwright 等效物小 93%
- **优势**：大幅减少 token 消耗

#### Passmark (Bug0)
- **工作方式**：将 AI 发现与测试执行分离——首次运行用 AI 发现操作并缓存，后续运行以零 LLM 调用回放缓存的 Playwright 操作
- **断言策略**：多模型共识（Claude + Gemini + 仲裁模型），减少误报
- **差异化**：后续执行零 LLM 成本

#### Expect (Bug0)
- **工作方式**：从 git diff 自动生成测试，作为 CLI skill 嵌入编码 Agent（Claude Code、Cursor、Gemini CLI）
- **流程**：读取 diff → 生成测试计划 → 在真实浏览器中通过 Playwright 执行
- **上线时间**：2026 年 3 月
- **差异化**：无需手动编写或维护测试，直接从代码变更推导

#### Docket
- **工作方式**：**视觉优先 + 坐标点击**——用视觉识别定位 UI 元素的 (X,Y) 坐标，直接点击屏幕坐标而非 DOM selector
- **优势**：测试在 UI 重构后不会断裂；启动时间数小时（而非 DOM 工具的数天/数周）
- **局限**：对精确像素位置有依赖，屏幕分辨率变化可能影响

#### Autonoma AI
- **工作方式**：开源端到端测试平台，使用 AI Agent 循环——截图 → LLM 决定操作 → 执行 → 重复，直到测试完成
- **特点**：零测试代码，只需自然语言描述；视觉模型定位 UI 元素（非 CSS/XPath）→ 自愈
- **跨平台**：Web（Playwright）+ iOS/Android（Appium），真实设备
- **技术栈**：TypeScript 96.5%，Node.js 24，React 19，PostgreSQL，Redis，K8s，Temporal
- **定价**：开源

### 2.3 代码优先 / 流量录制平台

#### Keploy
- **工作方式**：使用 **eBPF** 在网络层捕获 API 调用、数据库查询、流事件——**零代码侵入、语言无关**（Go / Java / Node.js / Python / Rust / C# 等）
- **测试生成**：录制真实用户流量 → 确定性重放 → 自动生成 API 和集成测试，目标 90% 覆盖率
- **AI 增强**：分析已有录制 + OpenAPI/Swagger schema，自动补充边界值、缺失字段、类型不匹配、乱序等测试
- **Mock 虚拟化**：不仅 mock HTTP API，还 mock 数据库（Postgres/MySQL/MongoDB）、消息队列（Kafka/RabbitMQ）
- **成熟度**：Apache-2.0 开源，17.6k GitHub Stars，2.2k Forks，593 个 release（最新 v3.5.62）
- **定价**：开源免费

#### Qodo Cover (原 CodiumAI Cover-Agent)
- **工作方式**：迭代式架构（Test Runner → Coverage Parser → Prompt Builder → AI Caller），自动生成提升覆盖率的单元测试
- **语言支持**：Python, Go, Java
- **LLM 支持**：通过 LiteLLM 集成 100+ LLM（OpenAI / Vertex AI / Azure 等）
- **注意**：**2025 年 6 月 15 日已停止维护**（5.4k Stars），建议 fork 使用
- **商业版**：Qodo CI（GitHub Action），2024 年 12 月推出免费预览

---

## 3. 开源工具与框架

| 项目 | 核心能力 | Stars | 状态 |
|------|---------|-------|------|
| **Autonoma AI** | 视觉 Agent 循环，零代码 E2E 测试 | 活跃 | 生产可用 |
| **Keploy** | eBPF 流量录制 + AI 测试扩展 | 17.6k | 生产可用，非常活跃 |
| **Qodo Cover** | 迭代式单元测试生成 | 5.4k | ⚠️ 已停止维护 |
| **Playwright MCP** | 浏览器 Agent 控制协议 | 微软维护 | 活跃 |
| **Stagehand** | 自愈浏览器测试 | Browserbase | 活跃 |

---

## 4. Self-Healing 测试：不只是 Selector 修复

这是一个常被过度简化的领域。关键发现：

**Selector 断裂只占测试失败的 ~28%**。完整的失败分布：

| 失败类型 | 占比 | 说明 |
|---------|------|------|
| 时序问题 | ~30% | 元素未加载完成、API 响应慢 |
| Selector 断裂 | ~28% | CSS/XPath 选择器失效 |
| 测试数据问题 | ~14% | 数据不存在或状态不对 |
| 视觉断言失败 | ~10% | 截图对比不匹配 |
| 交互失败 | ~10% | 点击/输入操作失败 |
| 运行时错误 | ~8% | 异常抛出 |

**大多数 "self-healing" 工具只解决 selector 修复（28%），剩下 72% 的问题仍然需要人工干预。** 更糟糕的是，只修 selector 的方法可能产生 **false pass**——当真正原因是 API 慢或数据错误时，修复 selector 反而掩盖了真实缺陷。

**正确的自愈架构**应该是三阶段：
1. **检测**：捕获 DOM 快照、网络活动、控制台日志等运行时 artifact
2. **诊断**：分类根因（selector? timing? data? runtime?）
3. **修复**：针对不同类别应用不同修复策略

**前沿做法**：
- LLM 驱动的**语义级视觉回归**：理解截图的语义而非逐像素对比——反锯齿差异不是缺陷，缺失按钮才是
- **Intent-based healing**：从代码变更重新推导测试意图，而非简单重试 selector fallback
- 对于 AI 生成的应用（"vibe-coded" apps，用 Cursor / Bolt / v0 构建），UI 每周都在变，传统录制式测试完全无法跟上

---

## 5. CI/CD 集成：Agent 进入流水线

### GitHub Agentic Workflows (2026 年 2 月技术预览)

GitHub 推出了 **Agentic Workflows**，在 GitHub Actions 内运行编码 Agent（Copilot / Claude Code / OpenAI Codex）来自动化仓库任务：

- **6 个主要用例**之一就是 **Continuous Test Improvement**：Agent 评估测试覆盖率并自动添加高价值测试
- **定位**：补充传统 CI/CD 而非替代——创建一个 agent-only 子循环，用于传统 YAML workflow 难以实现的自主任务
- **安全模型**：纵深防御——默认只读权限、沙箱执行、工具白名单、网络隔离、PR 合并必须人工审批
- **成本**：每次运行通常消耗 2 个 premium request
- **意义**：标志着 Agent 测试从独立工具进入主流 CI/CD 平台

### PR 感知的 Agentic 测试生成

前沿做法是 **PR-aware test generation**：
1. 拦截 Pull Request payload
2. 识别代码 diff
3. 交叉引用 Jira 用户故事
4. 自主重新生成或更新受影响的 Gherkin 场景
5. 通过智能优先级排序实现 **40–60% 流水线执行时间削减**

### 多 Agent CI/CD 架构

专用 Agent 分工协作：
- **Code Analysis Agent**：分析变更影响面
- **Risk Assessment Agent**：评估风险等级
- **Strategy Selection Agent**：选择测试策略
- **Execution Orchestration Agent**：编排执行
- **Quality Decision Agent**：自主做出质量决策

报告的效果（注意：部分数据来自未提供独立验证的厂商文章，需谨慎看待）：
- 测试流水线耗时减少 78%
- 人工干预需求减少 89%
- 由不充分测试导致的生产事故减少 84%

---

## 6. 学术研究前沿

### 6.1 LLM 测试生成的系统性综述

**论文**：*"Large Language Models for Unit Test Generation: Achievements, Challenges, and Opportunities"* (arXiv:2511.21382, 2025-11)

对 **115 篇论文**的系统性综述，关键发现：

- **方法分布**：Prompt Engineering 占 89%，Fine-tuning 占 20%，Pre-training 仅 4%
- **核心问题**：原始 LLM 生成的测试**编译/执行通过率经常低于 50%**
- **解决方案**：迭代验证+修复循环已成为标准，可将通过率提升至 70%+
- **致命弱点**：当前工具为了最大化 pass rate 可能**主动过滤掉发现真实 bug 的失败测试**（Oracle Problem）
- **上下文限制**：42% 的生成失败归因于缺失外部上下文；C++ 等复杂环境中编译成功率可低至 10%
- **增长速度**：从 2021–2022 年每年 1 篇 → 2024 年 55 篇，近指数增长

**另一篇综述** (arXiv, 2025-09)：130 篇文章，分为 7 个类别：
- 单元测试生成（40 篇）
- 高级测试生成（28 篇）
- Oracle 生成（26 篇）
- 反思/评估（23 篇）
- 测试增强（12 篇）
- **Test Agents**（8 篇）—— 新兴类别
- 非功能测试（8 篇）

GPT 模型家族在 35% 的论文中被使用，Llama 家族 18.3%。

### 6.2 Agent 生成的测试真的有用吗？

**论文**：*"Rethinking the Value of Agent-Generated Tests for LLM-Based Software Engineering Agents"* (arXiv:2602.07900, 2026-04)

**颠覆性发现**：

| 模型 | 写测试比例 | 任务解决率 |
|------|----------|-----------|
| GPT-5.2 | 0.6% | 71.8% |
| Claude Opus 4.5 | 83% | 74.4% |

- GPT-5.2 几乎不写测试但解决率相当 → **Agent 生成的测试并不显著提升任务解决率**
- Prompt 干预实验（增加/减少测试编写行为）在 4 个模型上**均未产生统计显著的结果差异**（McNemar test, p > 0.05）
- Agent 写的"测试"主要是 **print 语句（观察性调试）**而非 assertion-based 验证
- **结论**：测试生成消耗了交互预算（token/时间），但没有带来等比例的收益

**对我们的启示**：自动测试生成的价值可能不在于帮助 Agent 解决当前任务，而在于为**后续回归测试**提供保障。

### 6.3 Agentic Property-Based Testing

**论文** (arXiv, 2025-10)：基于 Claude Opus 4.1 + Claude Code 的自主属性测试 Agent

- 在 **100 个流行 Python 包**（933 个模块）上自主生成了 **984 个 Bug 报告**
- 84.2% 的模块中发现了问题
- 手动审查 50 份报告：**56% 是真实 bug**，32% 值得上报给维护者
- 排名前 21 的报告中：**86% 有效，81% 值得上报**
- **真实案例**：
  - NumPy PR #29609：Wald 分布负样本 bug
  - AWS Lambda Powertools PR #7246：字典分块 bug
  - HuggingFace Tokenizers PR #1853：HSL 颜色格式解析错误
- 成本：总运行 136.6 小时，API 费用 $5,474.20（**$5.56/bug report**）
- 相比 2023 年 Vikram 等人的非 Agentic 方法（40 个函数上 41% 运行成功），Agent 方法在规模和有效性上有质的飞跃

### 6.4 工业案例：人机协作测试

**论文** (2026-03)：Hacon（西门子子公司）的工业案例研究

- 使用 RAG + 多 Agent（Generator / Evaluator / Reporter）自动生成回归测试脚本
- **30–50% 的 AI 生成代码被测试工程师原样保留**
- 但：49 个 AI 生成脚本中——15 个被完全重写，20 个需要重大修改，13 个中等修改，仅 1 个只需小改
- 常见问题：硬编码数据（31/49）、冗余导入（29/49）、未使用对象（23/49）
- **关键发现**：工程师经常拒绝技术上正确但语境不合适的代码，偏好已有的可信代码片段
- 治理模型："**静默 AI 队友**"——有限自主权，不能未经人工批准将脚本加入回归套件
- **自动化差距**：手动测试占总测试的 82–87%，每次发布增长 10–20%，而自动化覆盖每次发布仅增长 1–2%

**实践者调查** (2025-10)：15 名测试从业者的定性研究
- 60%（9/15）报告 **准确性和幻觉** 是最大挑战
- 从业者并非将 LLM 视为完全自主的测试工具，而是通过**迭代式五步人工监督流程**使用
- 最常见用途：测试用例创建（8/15）、测试自动化（7/15）
- 7/15 担心数据隐私，3/15 担心对 AI 的过度依赖会侵蚀测试者的分析判断力

### 6.5 SWE-Bench 与 Agent 能力评估

**SWE-Bench Pro** (2025-09)：更真实的 Agent 评估基准

- 1,865 个问题，来自 41 个仓库，平均补丁 107.4 行、4.1 个文件
- 顶级 Agent 在 SWE-Bench Verified 上 >70% → **SWE-Bench Pro 上仅 ~23%**
- 没有需求/接口增强时，GPT-5 从 25.9% 降至 8.4%，Claude Opus 4.1 从 22.7% 降至 8.2%
- 在商业私有代码库（18 个创业公司代码库）上，最佳模型仅 17.8%

**ICSE 论文趋势**：标题含 "agent" 的论文从 ICSE 2025 的 7 篇增至 ICSE 2026 的 **30 篇**。

**可复现性危机**：大多数研究不公开 prompt、temperature、精确 LLM 版本号。论文提出发布 **Thought-Action-Result (TAR) 轨迹**作为开放数据，以实现低成本跨方法对比。

---

## 7. 人机协作：解决 Overlap 问题

这是当前最缺乏系统化解决方案的领域。综合各方研究，前沿做法是：

### 7.1 分层测试金字塔 + AI 能力映射

```
                    ┌──────────────┐
                    │  探索性测试   │  ← 人类主导（直觉、领域知识、创造力）
                    │  + 可用性测试  │
                   ┌┴──────────────┴┐
                   │  E2E / 视觉回归 │  ← AI Agent（浏览器驱动、视觉对比）
                  ┌┴────────────────┴┐
                  │  集成测试 / API 测试│  ← AI + 流量录制（Keploy）
                 ┌┴──────────────────┴┐
                 │    单元测试          │  ← AI 生成（但 Oracle Problem 仍在）
                 └────────────────────┘
```

### 7.2 减少 Overlap 的策略

1. **PR-aware 智能调度**：分析代码变更 → 只运行受影响的测试 → 减少 40–60% 流水线时间
2. **AI 负责可重复的机械验证**：回归测试、跨浏览器/设备兼容性、视觉回归
3. **人类负责高判断力活动**：探索性测试、可用性评估、业务逻辑验证、安全审计
4. **"静默 AI 队友"模式**：AI 生成初稿 → 人类审核和批准 → AI 不能自主添加到回归套件
5. **Coverage Gap 分析**：AI 分析现有测试覆盖情况，指出人类应重点关注的**未覆盖区域**

### 7.3 治理框架

Hacon 的工业实践给出了一个可参考的治理模型：
- AI 的所有输出（包括执行轨迹）记录在 MLflow 中
- AI 具有**有限自主权**——可以生成、执行、报告，但不能发布
- 人类保留最终的"加入回归套件"决策权
- 这避免了 AI 测试和人类测试的无效重叠

---

## 8. 关键限制与未解决问题

### 8.1 技术限制

| 问题 | 现状 |
|------|------|
| **Oracle Problem** | 最根本的挑战——如何自动判断"预期结果是什么"？当前工具倾向于让测试通过而非发现 bug |
| **幻觉断言** | AI 生成的断言可能不匹配实际应用行为 |
| **上下文窗口** | 42% 的生成失败源于缺失外部上下文；复杂项目中尤为严重 |
| **模型耦合风险** | AI provider 升级模型版本可能导致工具集成断裂 |
| **测试爆炸** | 无人监管时 Agent 可能生成大量低价值测试 |
| **编译通过率** | 原始 LLM 生成的测试通过率常 < 50%，需要迭代修复 |

### 8.2 实践限制

| 问题 | 现状 |
|------|------|
| **数据隐私** | 47%（7/15）的从业者担心将敏感数据送入外部 LLM API |
| **认知过度依赖** | 对 AI 建议的过度依赖可能侵蚀测试者的分析判断力 |
| **"技术正确但语境不合适"** | AI 生成的代码常被工程师拒绝，因为不符合团队的维护性/领域惯例 |
| **可复现性** | 大多数研究不公开 prompt、temperature、精确模型版本 |
| **评估不充分** | 仅 1/18 的 Agentic AI 论文与相关 SOTA Agent 基线做了对比 |

### 8.3 未解决问题

1. **全生命周期自主测试仍不存在**：2026 年大多数平台仍需要客户自己管理至少一个环节——环境配置、测试数据、规约编写、CI 接入或失败分诊
2. **集成测试和系统测试的 Agent 化**：大多数工具集中在单元测试或 UI E2E 测试，中间层的集成测试 Agent 化研究严重不足
3. **跨服务/微服务场景**：当系统涉及多个服务时，Agent 如何理解全局行为仍是开放问题

---

## 9. 对我们项目的启示

结合 Claude Code Manager 的实际情况（调度多个 Claude Code 实例并行工作），以下是可落地的方向：

### 9.1 短期可行（现有工具整合）

1. **Keploy 集成**：在 CI/CD 中使用 Keploy 录制 API 流量 → 自动生成集成测试，零代码侵入。特别适合我们的 FastAPI 后端
2. **Playwright MCP + Claude Code**：利用 Playwright 的 MCP Server 让 Claude Code 实例直接驱动浏览器进行 E2E 测试验证
3. **Expect / Bug0 风格的 PR-aware 测试**：在 Dispatcher 中增加一步——当 Claude Code 完成任务后，自动从 diff 推导并执行相关测试

### 9.2 中期方向（系统设计）

1. **分层测试责任矩阵**：
   - Claude Code Agent 负责：单元测试生成、API 回归测试、类型检查
   - 人类负责：探索性测试、可用性、业务逻辑正确性
   - 系统记录哪些测试由谁负责，避免重叠
2. **测试覆盖 Gap 分析 Agent**：定期运行一个 Agent 分析当前测试覆盖情况，输出人类应该关注的未覆盖区域
3. **自愈测试 Pipeline**：借鉴 QA Wolf 的三阶段诊断模型（检测 → 分类 → 修复），而非简单重试

### 9.3 长期愿景

1. **Goal-based Testing**（与我们的 Goal 模式高度吻合）：用自然语言描述测试目标，让 Agent 自主规划和执行测试，通过 Goal Evaluator 判断是否达成
2. **MCP 集成的测试验证**：让 Claude Code 实例在完成编码任务后，通过 MCP 直接调用测试 Agent 验证自己的工作
3. **Agentic Property-Based Testing**：参考 arXiv 论文的方法，在我们管理的项目上自动运行属性测试发现 bug

---

## 附录：核心参考来源

### 学术论文
- *"Rethinking the Value of Agent-Generated Tests for LLM-Based SE Agents"*, arXiv:2602.07900, 2026-04
- *"Large Language Models for Unit Test Generation: Achievements, Challenges, and Opportunities"*, arXiv:2511.21382, 2025-11
- *"SWE-Bench Pro"*, arXiv, 2025-09
- *"LLMs for Software Testing: A Semi-Systematic Literature Review"* (130 articles), arXiv, 2025-09
- *"Agentic AI for Software Engineering"* (ICSE evaluation practices), arXiv:2026-04
- *"Agentic Property-Based Testing"* (Claude Code-based), arXiv, 2025-10
- *"Industrial Case Study at Hacon (Siemens)"*, 2026-03
- *"LLM-Driven Testing Practitioner Study"* (n=15), 2025-10
- *"AI-Integrated Test Pyramid Framework"*, 2025-12
- *"Multi-Agent LLM Testing Framework with Google Gemini"*, 2024-12

### 行业来源
- QA.tech: *"The 13 Best AI Testing Tools in 2026"*, 2026-04
- Shiplight AI: *"Best Agentic QA Tools in 2026"*, 2026-05
- Mabl: *"AI Agents in CI/CD Pipelines for Continuous Quality"*, 2026-01
- TestQuality: *"Agentic QA Architecture: Reasoning Loops, Self-Healing DOM & Autonomous Testing"*, 2026-03
- GitHub Blog: *"Agentic Workflows Technical Preview"*, 2026-02
- Playwright AI Agent Ecosystem 综述, 2026-04
- AI Browser Testing Tools 对比 (Stagehand / Agent-Browser / Passmark / Expect), 2026-04
