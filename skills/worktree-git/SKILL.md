---
name: worktree-git
description: >
  使用 git worktree 隔离开发，在独立工作区完成功能后合并回主分支。
  适用于需要代码隔离的任务开发场景。
when_to_use: >
  任务需要修改代码时激活。确保在 worktree 中工作，避免影响主分支。

ccm:
  always: false
  priority: 7
  version: 1
  tags: [git, worktree, development]
  tools: []
---

## Git Worktree 工作流

所有代码修改必须在 git worktree 中进行，完成后合并回主分支。

### 标准流程

1. **创建 worktree**
   ```bash
   git worktree add -b task-<描述> .claude-manager/worktrees/task-<描述> main
   ```

2. **在 worktree 中工作**
   - 进入 worktree 目录
   - 编写代码、运行测试
   - 不要在 worktree 中切换分支

3. **提交代码**
   ```bash
   git add <files>
   git commit -m "简洁描述改动"
   ```

4. **合并回主分支**（如有 remote）
   ```bash
   git fetch origin main
   git rebase origin/main
   git checkout main && git merge <task-branch>
   git push origin main
   ```

5. **清理**
   ```bash
   git worktree remove .claude-manager/worktrees/<worktree名>
   git branch -D <task-branch>
   ```

### 冲突处理

rebase 发生冲突时：
1. `git diff --name-only --diff-filter=U` 查看冲突文件
2. 逐个解决冲突
3. `git add <resolved-files> && git rebase --continue`
4. 无法解决则 `git rebase --abort`

### 注意事项

- 通过 `git remote -v` 判断是否有 remote
- 有 remote → 必须 rebase + push
- 无 remote → 跳过 fetch/push 步骤
- 完成后必须清理 worktree 和分支

## Lessons Learned
<!-- 自进化系统自动追加 -->
