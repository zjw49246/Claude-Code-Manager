---
name: task-lifecycle
description: >
  CCM 任务的 9 步生命周期管理。从领取任务到完成清理的完整流程规范。
when_to_use: >
  执行 CCM 分配的任务时激活。确保按照标准流程完成任务的每个阶段。

ccm:
  always: true
  priority: 9
  version: 1
  tags: [task, lifecycle, workflow]
  tools: []
---

## 任务生命周期（9 步流程）

收到任务后，按以下流程自主完成：

1. **领取任务** — 阅读项目 CLAUDE.md 和代码，理解上下文
2. **创建工作区** — 使用 git worktree 创建隔离分支
3. **实现功能** — 编写代码，确保可运行
4. **提交代码** — git add + git commit，commit message 简洁
5. **Merge + 测试** — 集成最新代码，运行测试
6. **自动合并到 main** — rebase + merge + push（有 remote 时）
7. **标记完成** — 更新文档（必须在清理之前）
8. **清理** — 删除 worktree 和任务分支
9. **经验沉淀** — 在 PROGRESS.md 记录经验教训

### 测试规范

- 改代码前先跑测试确认基线
- 改代码后再跑一遍确认无回归
- 新增功能同步新增测试用例
- 修 bug 先写复现测试再修复

### 文件维护

每次功能变更后同步更新：
- CLAUDE.md — 架构、约定变化时
- README.md — 功能、使用流程变化时
- TEST.md — 新增功能时添加测试文档

## Lessons Learned
<!-- 自进化系统自动追加 -->
