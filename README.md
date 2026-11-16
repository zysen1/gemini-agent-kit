# Gemini Agent Kit

**把仓库链接发给你的 Agent，让它装好网关，把 Gemini 接给自己使用。**

仓库提供一个本机面板，用于导入授权账号、选择模型和获取 API 连接信息。上游由独立安装的社区项目 Antigravity Tools 提供；不需要先申请 Google AI Studio 项目密钥。

可以把仓库交给 **Muse、GrokBot、Codex、WorkBuddy、豆包工作、DSH** 等 Agent。它先检查自身能力，再选择自定义 API、命令／MCP 外援或授权设备文件桥；有命令工具就能先用一次 `ask` 调用，不必额外安装 MCP。

复制这一段，发给你习惯的 Agent：

```text
请读取 https://github.com/zysen1/gemini-agent-kit 的 AGENTS.md，
把 Gemini 接给你自己使用，直接完成安装和接入，不要只给教程。
已有授权网关就复用；账号只让我在本机界面输入。
按你的实际能力选自定义 API、MCP／命令或授权文件桥；
我没要求替换主模型时，把 Gemini 作为外援，保留原默认模型。
接完做一个短任务核对，告诉我怎么用、怎么停止和恢复。
```

只需三步：

1. **准备服务**：让 Agent 安装并启动面板，点击「准备并启动」。也可双击 `启动面板.command`（Mac）或 `启动面板.bat`（Windows）。需要 Python **3.11+**；Windows 首次按提示安装上游组件。
2. **导入账号**：只在本机页面填写你授权使用的完整 Refresh Token（`1//` 开头），不用粘贴账号密码。
3. **让 Agent 接好并使用**：它读取真实模型目录，按自身能力完成连接和一次短任务。想换成 Gemini 主模型时直接告诉它；否则保留原默认、把 Gemini 作为外援。**Codex 是支持的常用选项**，其他产品按[接手流程](docs/AGENT_HANDOFF.md)判断能力，不需要重复指定“接给谁”。

自己启动：

```sh
git clone https://github.com/zysen1/gemini-agent-kit.git
cd gemini-agent-kit
python3 scripts/kit_app.py --open
```

面板默认在 **http://127.0.0.1:8787**，API Base URL 为 **http://127.0.0.1:8787/v1**。单独启动面板不会修改客户端配置；复制上述口令则让接手的 Agent 完成自己的连接。保持启动窗口运行；`Ctrl+C` 停止本工具管理的服务。密钥只进入本机客户端凭据设置，不发到聊天。

请求默认串行，**不额外固定等待 120 秒**。遇到上游限流会遵守实际返回的等待提示；没有提示时不猜恢复时间、不自动重试。网关不会增加账号额度。

本机地址只能在同一台电脑使用。云端 Agent 的 `127.0.0.1` 指向云端自己；请在它的运行环境部署，或使用已有的鉴权 HTTPS 网关。

[面板使用与恢复](docs/LOCAL_PANEL.md) · [完整 Agent 指令](PROMPT.md) · [Agent 接手流程](docs/AGENT_HANDOFF.md) · [客户端指南](docs/CLIENTS.md) · [模型与额度](docs/MODELS_AND_LIMITS.md)

运行组件采用上游 Antigravity Tools **5.0.3** 发布版，并核验下载摘要。本仓库原创代码采用 MIT；独立上游组件采用 **CC BY-NC-SA 4.0**，不被本仓库的 MIT 许可覆盖，见[第三方声明](THIRD_PARTY_NOTICES.md)。本项目不提供账号或额度，也不保证每个客户端、每种订阅或所有模型均可用。网关展示的型号是路由信息，不是独立的模型来源鉴定。

<details>
<summary>已有 Magpie / CLI、验证与历史记录</summary>

原有 CLI 支持 Python 3.10+。已有网关可直接使用，无需重新部署：

```sh
python3 scripts/agent_kit.py doctor --base-url "你的网关地址/v1"
python3 scripts/agent_kit.py models --base-url "你的网关地址/v1"
```

这两个命令不发推理请求。需要真实验证时，`verify` 最多发送 3 次短请求并消耗额度；它通过也不代表目标客户端的真实任务已经通过。

- 使用现有 Manager：[独立接入说明](docs/ANTIGRAVITY_MANAGER.md)。
- 主模型、自定义客户端与 MCP 外援：[客户端指南](docs/CLIENTS.md)。
- 无法直接访问本机 API 的宿主：[文件桥接](docs/FILE_BRIDGE.md)。
- 需要单独部署串行代理：[请求排队说明](docs/PACED_PROXY.md)。不要沿用历史演示的固定 120 秒间隔作为新安装默认值。
- 历史环境、成功次数与失败原因：[实测记录](docs/STATUS.md)。旧测试不代表当前账号或其他人的环境。

开发检查：`python3 -m unittest discover -s tests -v`。这些本地模拟测试不会调用真实 Gemini，也不能替代客户端任务验收。

</details>
