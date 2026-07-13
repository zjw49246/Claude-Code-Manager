# CCM Vibe Coding 面试题

> 这是一道 **vibe coding** 面试题：你将通过与 AI 协作，给一个真实运行中的系统 **Claude Code Manager (CCM)** 加一个功能。我们关注的是你**理解陌生代码库、拆解需求、驱动 AI、验证结果**的综合能力。

---

## 一、环境

### 1.1 两个服务（都给你，地址不同）

| 用途 | 地址 | 登录 Token |
|---|---|---|
| **你的工作实例**（在这里改代码 + 重启 + 验证） | **https://interviewee.claude-code-manager.com** | `2a5a9000048ad51300bbdec867fe3f1187d307f18b23d21c` |
| **参照实例**（干净对照，**请勿修改它的代码或配置**） | **https://interviewer.claude-code-manager.com** | `4701a74336251c077e6a84659382c1dcbeb3d833566497b3` |

打开网址 → 输入对应 Token 登录。你所有改动都做在**工作实例 (interviewee)** 上；参照实例保持原样别动。

### 1.2 关于“提交代码”

这个环境**没有配置 GitHub 凭证**，推不到真实的 GitHub。演示与验证时，用**本地 git 仓库**即可（本地 commit、指向本地 remote 的 push 都能正常跑）。

### 1.3 代码在哪、怎么跑

- CCM 的源码在工作实例上：`~/ccm-interviewee`（后端 Python/FastAPI，前端 React/Vite，包管理用 `uv`）。
- **先读 `CLAUDE.md` 和 `README.md`** 了解这个项目是什么、怎么运行、有哪些约定。
- 改完后让改动生效并自测：
  ```bash
  cd ~/ccm-interviewee
  uv run python -m pytest backend/tests/ -v     # 后端测试
  cd frontend && npx tsc --noEmit && npm run build   # 前端类型检查 + 构建（若动了前端）
  systemctl --user restart ccm-interviewee      # 重启工作实例
  ```
  然后打开 **https://interviewee.claude-code-manager.com** 验证。

---

## 二、任务

### 2.1 目标

给 CCM 加一个功能：让它在**每次要向 GitHub 提交代码之前（不管是 commit、push，还是开 PR），都先对涉及到的代码做一次 review，把 review 结果返回给用户，由用户根据结果来决定接下来怎么做。**

行为逻辑：

- **还没 review 过** → 先做 review，把结果返回给用户，**停下来等用户指令**（这一步先不要真的提交）。
- **已经 review 过** → 按**用户的指令**执行相应的提交操作。

### 2.2 要求

- 做成一个 **CCM 的 Skill（技能）**，创建任务时能**勾选开关**；关掉时提交行为恢复正常、不拦截。
- review 要**真的分析本次涉及的代码/改动**，不能是假的占位输出。
- review 结果要**在对话里返回给用户**，由用户拍板。
- **commit / push / 开 PR 三种情况都要覆盖。**

### 2.3 验收

在工作实例 (interviewee) 上：开启该 Skill 的任务，让 AI 改一处小代码并提交 —— 期望它**先给出一份 code review、在你回复之前不提交**；你回「通过」→ 提交发生，你回「有问题 / 要改」→ 不提交、去修改。不开这个 Skill 的任务 → 正常提交、无拦截。（提交用本地 git 演示即可。）

### 2.4 交付

- 代码改在 `~/ccm-interviewee`，部署重启后能**当场演示** 2.3 的验收流程。
- 简单讲讲：你的实现思路、遇到的坑、以及如果时间更充裕你会怎么做得更好。

---

祝顺利，享受 vibe coding 🚀
