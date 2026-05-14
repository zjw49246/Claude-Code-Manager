# PTY 交互模式运行 Claude Code

通过 PTY（伪终端）以交互模式运行 Claude Code CLI 的技术方案。与 `-p` 非交互模式不同，PTY 模式启动一个**长驻的交互式进程**，通过 stdin 发送 prompt，通过 session JSONL 文件读取结构化响应。

## 核心原理

```
Python 后端
│
├── PTY master fd ──write──→ PTY slave (stdin)  ──→ Claude Code 交互进程
│                                                       │
│                                                       ├─→ TUI 渲染到 PTY stdout (忽略)
│                                                       │
│                                                       └─→ JSONL 转录文件 (实时写入)
│                                                                │
└── JSONL File Watcher ←─────────────────────────────────────────┘
     读取结构化 JSON 消息
```

PTY 是操作系统内核级别的终端抽象。Claude Code 通过 `isatty(stdout)` 判断运行模式——PTY slave 是一个真实的 TTY 设备（`/dev/pts/N`），因此 Claude Code 以完整的交互模式启动，具备全部功能：TUI 界面、工具调用、session 持久化、上下文延续。

## 与 `-p` 模式的对比

| | `-p` 非交互模式 | PTY 交互模式 |
|--|--|--|
| 进程生命周期 | 每轮一个新进程 | 一个长驻进程，多轮复用 |
| 多轮上下文 | 需要 `--resume` + session_id | 天然连续，同一进程内 |
| 输出格式 | `--output-format stream-json` (stdout) | session JSONL 文件 (磁盘) |
| TUI | 无 | 完整渲染（但可忽略） |
| 中途中断 | SIGTERM 杀进程 | ESC 键 / Ctrl+C via PTY |
| 工具调用 | 全部可用 | 全部可用 |
| Session 持久化 | 可选 | 自动 |

## 前置条件

### Claude Code 用户设置

在 `~/.claude/settings.json` 中配置：

```json
{
  "skipDangerousModePermissionPrompt": true
}
```

这会跳过每次启动时的 workspace trust 确认对话框。不配置的话需要在 PTY 中自动发送 Enter 来确认。

### 环境变量清理

启动子进程前必须清除以下环境变量，避免嵌套 session 检测或继承父进程状态：

```python
VARS_TO_CLEAN = [
    'CLAUDECODE',
    'CLAUDE_CODE',
    'CLAUDE_CODE_ENTRYPOINT',
    'CLAUDE_CODE_SESSION_ID',
    'CLAUDE_CODE_EXECPATH',
    'CLAUDE_EFFORT',
    'CLAUDE_AGENT_SDK_VERSION',
    'CLAUDE_CODE_AGENT',
    'CLAUDE_CODE_SESSION_NAME',
    'CLAUDE_CODE_SESSION_LOG',
    'CLAUDE_CODE_SIMPLE',
    'CLAUDE_JOB_DIR',
    'AI_AGENT',
]
```

## 实现参考

### PTY 进程管理

```python
import pty, os, select, fcntl, termios, struct, uuid, time, json, signal

class PTYInstance:
    """管理一个长驻的 Claude Code 交互式 PTY 进程"""

    def __init__(self, cwd: str, session_id: str | None = None):
        self.session_id = session_id or str(uuid.uuid4())
        self.cwd = cwd
        self.master_fd: int | None = None
        self.pid: int | None = None

    def spawn(self):
        master, slave = pty.openpty()

        # 设置终端尺寸（rows, cols, xpixel, ypixel）
        winsize = struct.pack('HHHH', 50, 200, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)

        pid = os.fork()
        if pid == 0:
            # 子进程：建立新 session，绑定 PTY slave
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(master)
            os.close(slave)

            # 清理环境变量
            for key in list(os.environ.keys()):
                upper = key.upper()
                if any(x in upper for x in ['CLAUDE', 'CLAUDECODE', 'AI_AGENT']):
                    del os.environ[key]

            os.environ['TERM'] = 'xterm-256color'
            os.chdir(self.cwd)

            os.execvp('claude', [
                'claude',
                '--dangerously-skip-permissions',
                '--session-id', self.session_id,
            ])
        else:
            # 父进程：持有 master fd
            os.close(slave)
            self.master_fd = master
            self.pid = pid

    def send_prompt(self, text: str, char_delay: float = 0.02):
        """逐字符发送 prompt 并按 Enter 提交"""
        for ch in text:
            os.write(self.master_fd, ch.encode())
            time.sleep(char_delay)
        time.sleep(0.1)
        os.write(self.master_fd, b'\r')

    def send_interrupt(self):
        """发送 ESC 键中断当前操作"""
        os.write(self.master_fd, b'\x1b')

    def drain_pty(self, timeout: float = 1.0):
        """清空 PTY 输出缓冲区（防止阻塞）"""
        buf = b''
        start = time.time()
        while time.time() - start < timeout:
            r, _, _ = select.select([self.master_fd], [], [], 0.2)
            if r:
                try:
                    buf += os.read(self.master_fd, 16384)
                except OSError:
                    break
        return buf

    def stop(self):
        """停止 PTY 进程"""
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                time.sleep(2)
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
        if self.master_fd is not None:
            os.close(self.master_fd)

    @property
    def jsonl_path(self) -> str:
        """Session JSONL 转录文件路径"""
        project_hash = self.cwd.replace('/', '-')
        return os.path.expanduser(
            f'~/.claude/projects/{project_hash}/{self.session_id}.jsonl'
        )
```

### JSONL 响应追踪

Claude Code 将每条消息实时写入 session JSONL 文件，格式与 `-p --output-format stream-json` 高度一致。

```python
class JsonlTracker:
    """追踪 session JSONL 文件，读取新增的结构化消息"""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0

    def read_new_messages(self) -> list[dict]:
        """读取自上次调用以来的新消息"""
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            f.seek(self.offset)
            new_data = f.read()
            self.offset = f.tell()

        results = []
        for line in new_data.strip().split('\n'):
            if not line.strip():
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return results

    def wait_for_response(self, pty_instance: 'PTYInstance',
                          timeout: float = 60) -> dict | None:
        """阻塞等待 assistant 回复完成（stop_reason == end_turn）

        必须在等待期间持续调用 drain_pty() 清空 PTY 缓冲区，
        否则缓冲区满会导致 Claude Code 进程阻塞。
        """
        start = time.time()
        while time.time() - start < timeout:
            pty_instance.drain_pty(0.5)

            for msg in self.read_new_messages():
                if (msg.get('type') == 'assistant' and
                        msg.get('message', {}).get('stop_reason') == 'end_turn'):
                    return msg

            time.sleep(0.5)
        return None
```

### 完整的交互流程

```python
# 1. 创建 PTY 实例
pty_inst = PTYInstance(cwd="/path/to/project")
pty_inst.spawn()

# 2. 等待 Claude Code 启动（TUI 渲染完成）
time.sleep(8)
pty_inst.drain_pty(3)

# 3. 创建 JSONL 追踪器
tracker = JsonlTracker(pty_inst.jsonl_path)
tracker.read_new_messages()  # 跳过启动阶段的初始化消息

# 4. 发送第一条 prompt
pty_inst.send_prompt("实现用户登录功能")
response = tracker.wait_for_response(pty_inst, timeout=120)

# 5. 提取 assistant 回复内容
if response:
    for block in response['message']['content']:
        if block.get('type') == 'text':
            print(block['text'])
        elif block.get('type') == 'tool_use':
            print(f"[调用工具: {block['name']}]")

# 6. 等待 TUI 恢复输入状态
time.sleep(2)
pty_inst.drain_pty(2)

# 7. 发送后续 prompt（上下文自动延续）
pty_inst.send_prompt("加上单元测试")
response2 = tracker.wait_for_response(pty_inst, timeout=120)

# 8. 结束时停止进程
pty_inst.stop()
```

## JSONL 消息格式

Session JSONL 文件中每行是一个 JSON 对象，关键消息类型：

### User 消息

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": "实现用户登录功能"
  },
  "uuid": "9a1a5a35-...",
  "timestamp": "2026-05-14T13:42:41.677Z",
  "sessionId": "007c1594-...",
  "entrypoint": "cli",
  "userType": "external",
  "permissionMode": "bypassPermissions",
  "cwd": "/path/to/project"
}
```

### Assistant 消息

```json
{
  "type": "assistant",
  "message": {
    "role": "assistant",
    "model": "claude-opus-4-6",
    "content": [
      { "type": "text", "text": "我来实现登录功能..." },
      { "type": "tool_use", "name": "Edit", "id": "toolu_...", "input": { ... } }
    ],
    "stop_reason": "end_turn",
    "usage": {
      "input_tokens": 24190,
      "output_tokens": 856,
      "cache_creation_input_tokens": 24190,
      "cache_read_input_tokens": 0
    }
  },
  "uuid": "468a2b1c-...",
  "timestamp": "2026-05-14T14:09:51.501Z",
  "sessionId": "007c1594-...",
  "entrypoint": "cli"
}
```

### 关键字段

| 字段 | 说明 |
|------|------|
| `message.stop_reason` | `"end_turn"` 表示回复完成，可以发送下一条 prompt |
| `message.content[]` | 内容块数组，包含 `text`、`tool_use`、`tool_result` 等类型 |
| `message.usage` | token 用量统计 |
| `message.model` | 使用的模型 |
| `sessionId` | 当前 session ID |

## 实现要点

### 1. PTY 缓冲区必须持续排空

PTY 有固定大小的内核缓冲区。如果不读取 master fd，缓冲区满后 Claude Code 的 stdout 写入会阻塞，进程挂起。

**在任何等待循环中都必须调用 `drain_pty()`。**

### 2. 逐字符输入更可靠

Claude Code 的 TUI 基于 Ink（React for CLI）框架，通过 raw mode 处理键盘事件。逐字符发送（每字符 ~20ms 间隔）在所有轮次都稳定工作。一次性 bulk write 在第一轮可行但后续轮次可能丢失。

建议加入随机抖动使输入节奏更自然：

```python
import random

def send_prompt_natural(master_fd: int, text: str):
    for ch in text:
        os.write(master_fd, ch.encode())
        delay = random.gauss(0.05, 0.02)
        time.sleep(max(0.01, min(0.15, delay)))
    time.sleep(0.1)
    os.write(master_fd, b'\r')
```

### 3. 轮次之间需要等待

Claude Code 回复完成后，TUI 需要时间重新渲染输入框。建议在检测到 `stop_reason == "end_turn"` 后等待 2-3 秒再发送下一条。

### 4. 崩溃恢复：`--resume`

如果 PTY 进程异常退出，可以用 `--resume` 恢复 session：

```python
os.execvp('claude', [
    'claude',
    '--dangerously-skip-permissions',
    '--resume', session_id,   # 恢复已有 session
])
```

### 5. Session 文件位置计算

JSONL 文件路径规则：

```
~/.claude/projects/{project_hash}/{session_id}.jsonl
```

其中 `project_hash` 是 cwd 的路径替换 `/` 为 `-`，去掉开头的 `-`：

```python
def get_project_hash(cwd: str) -> str:
    return cwd.replace('/', '-').lstrip('-')
    # /home/ubuntu/my-project → home-ubuntu-my-project
```

实际路径以 `-` 开头（即替换后不去开头的 `-`）：

```python
def get_jsonl_path(cwd: str, session_id: str) -> str:
    project_hash = cwd.replace('/', '-')  # 保留开头的 -
    return os.path.expanduser(
        f'~/.claude/projects/{project_hash}/{session_id}.jsonl'
    )
```

## 可选：HTTP Stop Hook

配置 Stop Hook 可以在 Claude 每次回复完成后主动通知你的后端，作为 JSONL 轮询的补充：

在 `~/.claude/settings.json` 中添加：

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "http",
            "url": "http://localhost:8000/api/hooks/stop",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Hook 的 POST body 包含：

```json
{
  "hook_event_name": "Stop",
  "session_id": "007c1594-...",
  "cwd": "/path/to/project",
  "stop_hook_active": true,
  "last_assistant_message": "回复的文本内容..."
}
```

`last_assistant_message` 字段直接提供最后一条 assistant 消息的文本，无需解析 JSONL。

## 与现有架构的集成思路

当前 `InstanceManager` 通过 `asyncio.create_subprocess_exec` 启动 `-p` 模式的 Claude Code。迁移到 PTY 模式的核心改动：

1. **替换进程创建方式**：`create_subprocess_exec` → `pty.openpty() + os.fork()`
2. **替换输出消费方式**：stdout readline → JSONL file watcher
3. **替换消息发送方式**：每次创建新进程 → `send_prompt()` 写入已有 PTY
4. **简化 session 管理**：去掉 `--resume` 逻辑，上下文在同一进程内天然延续
5. **新增 PTY drain 循环**：持续清空 PTY 缓冲区防止进程阻塞

JSONL 消息的 `message.content` 结构与 `-p --output-format stream-json` 的 assistant message 高度一致，现有的 `StreamParser` 和前端渲染逻辑可大量复用。

## 已验证的能力

以下功能通过实验验证可正常工作（Claude Code v2.1.141）：

- [x] PTY 启动交互模式
- [x] `skipDangerousModePermissionPrompt` 跳过 trust dialog
- [x] 简单文本回复
- [x] 多轮上下文延续（4+ 轮）
- [x] 工具调用（Bash、Read、Edit 等）
- [x] JSONL 实时写入和读取
- [x] `stop_reason == "end_turn"` 作为回复完成信号
- [x] ESC 键中断正在进行的操作
- [x] 中断后继续对话
- [x] Session 元数据：`kind=interactive`、`entrypoint=cli`
