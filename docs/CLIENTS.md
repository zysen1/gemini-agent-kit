# 客户端接入

先确保 `doctor` 能读到目录，选择 `models` 返回的完整模型 ID。网关可访问、模型可调用、客户端能完成任务是三层不同检查。

| 客户端 | 本仓库路线 | 结果应如何描述 |
| --- | --- | --- |
| Codex CLI | 独立 OpenAI Responses provider | 此 CLI 会话使用指定网关模型 |
| WorkBuddy | Magpie 原生 adapter 或自定义 Chat Completions 模型 | 当前任务选用了网关模型 |
| Muse 桌面／网页产品 | 产品支持的命令／MCP，或已授权设备文件工具配合本机 worker | Agent 可以把指定子任务交给 Gemini；主模型不变 |
| Grok Bot | 所在运行环境中的命令或 MCP 工具入口 | Agent 可以把子任务交给 Gemini |

## Codex CLI

```sh
python3 scripts/agent_kit.py launch-codex --model "完整模型 ID" --dry-run
python3 scripts/agent_kit.py launch-codex --model "完整模型 ID"
```

启动器设置独立 `magpie_kit` provider、`base_url`、`env_key` 和 `wire_api="responses"`，将网关凭据传给子进程。不写全局 `config.toml`。退出该进程即可结束这一会话的配置。

额外 Codex 参数放在 `--` 后，例如：

```sh
python3 scripts/agent_kit.py launch-codex --model "完整模型 ID" -- exec "阅读当前目录的 brief.txt，生成三个不同开头并保存为 draft.txt"
```

在专用任务目录中运行并保留 Codex 的正常权限控制。先准备真实 `brief.txt`，检查输出，再修改材料重试。Chat Completions 的 `verify` 通过，不能替代这里的 Responses 实测。

Magpie 自带的 Codex 集成还可管理模型目录和桌面应用配置；那是另一路线，应按当前 [Magpie Codex 文档](https://usemagpie.ai/docs/codex-models) 操作。已有线程可能保留原 provider，需要新会话核验。本仓库 CLI 启动器不承诺修改桌面模型菜单。

## WorkBuddy

先按已选网关确定路线：使用 Antigravity Manager 时，直接配置下文的自定义模型，无需另外安装 Magpie；使用 Magpie 时，可打开其 Agents 页面，使用当前版本的 WorkBuddy 行连接模型。Magpie 源码包含腾讯 WorkBuddy 与国际版 WorkBuddy AI 的独立 adapter；实际可用性仍取决于安装版本。

模型出现在 WorkBuddy 选择器后，重启必要的应用并在新任务里选中它。写入模型列表不等于当前任务已经切换。

自定义模型路线：

```sh
python3 scripts/agent_kit.py plan --client workbuddy --model "完整模型 ID" --output reports/workbuddy-plan-01.json
```

在当前应用的自定义模型界面核对这些字段，界面文字可能随版本变化：

| 字段 | 填写方式 |
| --- | --- |
| API URL | Magpie 通常为 `http://127.0.0.1:3425/v1/chat/completions`；Manager 通常为 `http://127.0.0.1:8045/v1/chat/completions`；以实际监听地址为准 |
| 模型名称／ID | 目录返回的完整 `id` |
| API Key | 本机 Magpie 可用客户端标签；Manager 填其 API 网关密钥；远程网关用其真实密钥，均不填 Google Refresh Token |
| 工具调用 | 来源和模型支持时启用，并用真实任务验证 |

不要把 `chat/completions` 重复加两遍。若应用要求的是 Base URL 而不是完整 API URL，应按它的接口语义使用 `/v1`。

单账号连续工具请求触发冷却时，可在同机启用[排队代理](PACED_PROXY.md)，将完整 URL 改成 `http://127.0.0.1:8046/v1/chat/completions`，仍使用同一个网关密钥。保存后在当前任务核对选中的完整模型 ID，并重新执行真实任务；配置已保存、代理健康检查通过，都不是任务成功。

如果请求尚未发出就出现工作目录 `ENOENT`，先核对实际路径和访问权限。必要时在一个真实存在的简单英文目录中复验，以区分路径解析与上游模型问题，不据此推断所有中文目录都不支持。

目前 Magpie adapter 的配置目标为 `~/.workbuddy/models.json`（可被 `WORKBUDDY_CONFIG_DIR` 覆盖）；国际版为 `~/.workbuddy-ai`。已有文件可能是数组或包含 `models` 的对象，不要直接用示例覆盖整份配置。实现参考：[workbuddy.go](https://github.com/yetone/magpie/blob/main/internal/agent/workbuddy.go)。

## Muse / Grok Bot：工具接入

这里提供的是 stdio MCP 工具 `ask_gemini`。宿主 Agent 负责规划、工具选择、权限和最终交付；Gemini 完成明确交给它的文字任务。

```sh
python3 scripts/agent_kit.py plan --client grokbot --model "完整模型 ID" --output reports/grokbot-plan-01.json
python3 scripts/agent_kit.py plan --client muse --model "完整模型 ID" --output reports/muse-plan-01.json
```

在宿主实际运行环境放置仓库，并使用该环境的绝对路径。例如连接器若支持标准 `mcpServers` 配置，可以参考：

```json
{
  "mcpServers": {
    "gemini-agent-kit": {
      "command": "python3",
      "args": [
        "/path/to/gemini-agent-kit/scripts/agent_kit.py",
        "mcp",
        "--base-url", "http://127.0.0.1:3425/v1",
        "--model", "目录中的完整模型 ID"
      ]
    }
  }
}
```

路径与型号是占位内容，需替换；具体产品未必使用这个外层 JSON。使用 `plan --runtime-script` 可以写入宿主上的实际脚本绝对路径。需要远程鉴权时，通过连接器的安全环境配置传入 `GEMINI_AGENT_GATEWAY_KEY`，不要把密钥写进公开配置。

启动 stdio 服务后无普通终端输出属于正常情况，它等待 MCP 消息。客户端应先发现 `ask_gemini`，再调用一次，例如让它基于给定材料生成三个开头。工具参数为 `prompt` 和可选 `max_tokens`（1–8192，默认 1024）。

只有 HTTP 远程 MCP 接口的产品，不能直接使用这个 stdio 命令；需要其支持的 command connector、同环境桥接或已有托管部署。本仓库没有实现 HTTP MCP 服务，不会自动创建公网入口。

### Muse 只有设备文件工具时

本轮所用的 Muse 桌面产品能读写已授权的 Mac 文件，但没有本机 shell、通用 HTTP 或本机 stdio MCP 工具入口。这种环境可以使用[本机文件桥接](FILE_BRIDGE.md)：Muse 将完整任务写入专用请求队列，本机 worker 调用已登录网关，再由 Muse 读取真实返回并保存成果。`plan --client muse` 本身不会启动这条文件路线。

每个请求使用新 ID，保留原始请求、真实回包和实际产物。本机检查者将私有回执的请求／结果哈希与共享文件核对，不能仅凭共享 JSON 中的 `success: true` 判断。模型原稿若经过宿主排版或改写，应分别保存原稿与最终稿并说明改动。它证明的是 Muse 委托 Gemini 完成子任务，不证明 Muse 已更换主模型。

### Grok Bot 虚拟机与聊天调度分开验收

若 Grok Bot 已有虚拟机中的 Pi／Magpie 工作流，先检查原配置和实际模型目录，保留其他 provider。也可在该虚拟机部署用户授权的 Manager，让 Pi 访问同一虚拟机的回环地址。由操作者在虚拟机终端启动 Pi 得到产物，只证明虚拟机内这条调用路径；还要由 Grok Bot 聊天实际发起并收取结果，才算宿主调度通过。

外层 Grok Bot 的周额度与 Gemini 来源额度分别记录。虚拟机可用不等于聊天额度已恢复；不能把 Pi 在虚拟机完成的任务写成 Grok Bot 主模型已替换，或声称外层 Agent 可以无限使用。

### 同名 Muse 的区别

Magpie 源码中的 `muse` 是 **Meta Muse Code CLI**，不是所有名叫 Muse 的桌面或网页产品。它使用独立的 `~/.config/muse/settings.json` 与 `endpoint_transport`。不要因为看到 Magpie 有 Muse 行，就修改用户另一款 Muse 应用或宣称其主模型已更换。实现参考：[muse.go](https://github.com/yetone/magpie/blob/main/internal/agent/muse.go)。

### 云端地址

云端 Agent 的 localhost 指向云端自身。只有网关也在同一环境时，示例回环地址才成立。否则使用已有的鉴权 HTTPS 网关，在安全环境中传入密钥。先在连接器环境运行 `doctor`，再调试客户端，避免把网络问题当模型问题。
