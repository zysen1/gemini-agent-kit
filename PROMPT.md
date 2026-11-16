# 复制给你的 Agent

把下面一段发给能操作本机终端与文件的 Agent：

```text
请读取 https://github.com/zysen1/gemini-agent-kit 的 AGENTS.md，
直接帮我装好并打开本机面板，
不要只生成计划。

检查 Python 3.11+、系统和已有 Manager。
优先复用我已授权的 Manager；
没有就准备仓库支持的独立 Antigravity Tools 5.0.3 组件。
使用 python3 scripts/kit_app.py --open 启动面板，
按“准备服务 → 导入账号 → 连接 Codex”完成。
不要把申请 Google AI Studio 项目密钥作为必经步骤。

需要账号信息时，
让我只在本机面板输入自己授权的 Refresh Token，
不要让我贴到聊天，
不要从其他应用提取登录凭据。
已有 Manager 使用显式 --connect-manager-config 复用，
只读取它的网关配置，
不重新索取账号 Token。

读取真实模型目录，
让我选可用型号；
发送一次短请求核验，
遇到限流按服务提示等待，
不自动重试、换号或换模型。
预览后备份并添加 Codex 配置，
保留其他设置和当前默认模型。

请把环境准备、服务、账号与配置这些能做的步骤完成。
最后让我保存工作、重启 Codex，
在设置的网关（Gateways）中启用 Gemini Agent Kit，
再新建会话选择模型，
并用真实材料生成一个文件，
再改变输入复验。
若你无法操作 Codex 自身窗口，
把这一小步明确交给我。
不要把 CLI 成功写成桌面成功。

分别告诉我配置完成、网关检查和客户端真实任务的结果，
保留真实产物及恢复方法。
需要截图时只保存成功步骤，
并在采集前遮住账户、令牌和私有路径。
不要声称仓库自带额度或可以无限使用。
```

可以再加一句实际任务，例如：“用我提供的活动信息写三个不同风格的开头，保存为 `draft.txt`，不要补编事实。”

WorkBuddy、Muse、Grok Bot 等其他客户端按 [CLIENTS](docs/CLIENTS.md) 另行接入、逐个实测；面板的 Codex 配置成功不代表它们也已通过。
