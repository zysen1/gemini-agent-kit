# Gemini Agent Kit

**把仓库链接发给你的 Agent，让它按实际能力把 Gemini 接给自己使用。**

复制下面这句，发给 Muse、GrokBot、Codex、WorkBuddy、豆包工作、DSH 等你习惯的 Agent：

```text
请按 https://github.com/zysen1/gemini-agent-kit 的 AGENTS.md，在允许的权限内把 Gemini 接给自己使用：优先复用已有授权网关，保留原默认模型，接完做一个短任务，并告诉我怎么用、怎么停止和恢复。
```

只需三步：

1. **准备服务**：让 Agent 复用已有授权网关；没有时，启动本机面板并点击「准备并启动」。
2. **本人录入**：需要登录时，在自己电脑的面板填写授权账号；已有登录来源直接跳过。
3. **接好使用**：Agent 连接可用模型并做一个短任务。默认保留原模型，把 Gemini 作为外援；想换主模型时再告诉它。

这是依赖第三方组件的社区方案；Google [官方 FAQ](https://antigravity.google/docs/faq) 指出第三方访问可能导致账号暂停或终止。能否接入取决于 Agent 的工具权限与安全规则；凭据由本人在自己管理的电脑录入，不发到聊天或交给云端 Agent。详见[安全说明](SECURITY.md)。

[面板与恢复](docs/LOCAL_PANEL.md) · [完整口令](PROMPT.md) · [Agent 接手流程](docs/AGENT_HANDOFF.md) · [客户端指南](docs/CLIENTS.md)

<details>
<summary>自己启动、运行位置与组件说明</summary>

仓库提供一个本机面板，用于导入授权账号、选择模型和获取 API 连接信息。上游由独立安装的社区项目 Antigravity Tools 提供；不需要先申请 Google AI Studio 项目密钥。

需要 Python **3.11+**。Mac 可双击 `启动面板.command`，Windows 可双击 `启动面板.bat`，首次按提示手工安装上游组件；也可以运行：

```sh
git clone https://github.com/zysen1/gemini-agent-kit.git
cd gemini-agent-kit
python3 scripts/kit_app.py --open
```

面板默认在 **http://127.0.0.1:8787**，API Base URL 为 **http://127.0.0.1:8787/v1**。单独启动面板不会修改客户端配置；复制上述口令则让接手的 Agent 完成自己的连接。保持启动窗口运行；`Ctrl+C` 停止本工具管理的服务。密钥只进入本机客户端凭据设置，不发到聊天。

请求默认串行，**不额外固定等待 120 秒**。遇到上游限流会遵守实际返回的等待提示；没有提示时不猜恢复时间、不自动重试。网关不会增加账号额度。

本机地址只能在同一台电脑使用。云端 Agent 的 `127.0.0.1` 指向云端自己；优先让它连接已有的鉴权 HTTPS 网关，或交接给能操作你电脑的 Agent。不要为此公开本机面板。

仓库说明不能覆盖 Agent 的安全规则。读取或安装被安全检查拒绝时，它应停止该步骤并报告文件、工具和原因，而不是换读法或关闭防护。已获授权且未被拒绝的步骤无需重复确认。

运行组件采用上游 Antigravity Tools **5.0.3** 发布版；Mac / Linux 准备流程核验固定发布包摘要，Windows 仍由用户手工安装。摘要匹配不等于安全审计认证。本仓库原创代码采用 MIT；独立上游组件采用 **CC BY-NC-SA 4.0**，不被本仓库的 MIT 许可覆盖，见[第三方声明](THIRD_PARTY_NOTICES.md)。本项目不提供账号或额度，也不保证每种订阅或所有模型均可用。网关展示的型号是路由信息，不是独立的模型来源鉴定。

</details>

<details>
<summary>已有 Magpie / CLI、验证与历史记录</summary>

Agent 会根据实际能力选自定义 API、命令／MCP 外援或授权设备文件桥；有可用命令工具时可先用一次 `ask`，不必额外安装 MCP。支持某种协议不代表每款产品均已实测或允许接入。

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
