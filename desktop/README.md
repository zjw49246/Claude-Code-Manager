# CCM Quick Capture — macOS Menubar App

在 macOS 菜单栏常驻的截图快捷工具，截图后直接创建 CCM task。

## 安装

```bash
cd desktop
pip install -r requirements.txt
python ccm_capture.py
```

## 首次配置

1. 启动后菜单栏出现 📷 图标
2. 点击图标 → **Settings...** 
3. 填写 Server URL（默认 `https://xiaoyu.claude-code-manager.com`）和 Auth Token
4. 点击 **Test Connection** 验证连接 → **Save**

配置保存在 `~/.ccm_capture.json`。

## 使用

**方式一**：点击菜单栏 📷 → **Capture & Create Task**

**方式二**：按全局快捷键 `Cmd+Shift+S`

截图后弹出表单窗口：
- 截图预览
- 输入 Prompt（发送给 Claude Code 的指令）
- 选择 Project
- 点击 **Create Task**

创建成功后自动在浏览器打开对应 task 的 chat 页面。

## 开机自启

macOS 系统设置 → 通用 → 登录项 → 添加 `ccm_capture.py`（或将其包装为 .app）。

## 权限

首次使用时 macOS 会提示：
- **屏幕录制权限**：用于截图（`screencapture` 命令）
- **辅助功能权限**：用于全局快捷键监听（`pynput`）

请在"系统设置 → 隐私与安全性"中授权。
