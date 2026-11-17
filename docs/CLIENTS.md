# 把 Gemini 接进你的 Agent

把仓库链接发给 Muse、GrokBot、Codex、WorkBuddy、豆包工作或其他 Agent，并让它先读 [执行指引](AGENT_HANDOFF.md)。**接手的 Agent 按用户授权、宿主规则和实际工具完成配置**：默认保留原模型，用 Gemini 外援；用户明确要求替换主模型时再用支持的自定义 API。产品名本身不代表某个版本一定开放相同接口或安全检查一定放行。

读取、安装或调用被安全检查拒绝时，停止被拒步骤，报告文件、工具与原因，不更换读取方式、编码内容或关闭防护绕过。第三方组件与凭据处理边界见 [SECURITY.md](../SECURITY.md)。

## 先拿到三个值

默认使用仓库面板，无需另装排队代理。面板第三步提供：

| 字段 | 本仓库面板的默认值 |
| --- | --- |
| OpenAI 兼容 Base URL | `http://127.0.0.1:8787/v1` |
| 完整 Chat Completions URL | `http://127.0.0.1:8787/v1/chat/completions` |
| 完整 Responses URL | `http://127.0.0.1:8787/v1/responses` |
| Model / 模型 ID | 面板从真实目录读出的完整 ID；选中后复制 |
| API Key | 面板的「复制 API Key」按钮；只粘贴到本机客户端的密钥框 |

填写 **Base URL** 的位置只填到 `/v1`；填写**完整 API URL** 的位置才加 `/chat/completions` 或 `/responses`。API Key 是本机网关密钥，不是 Google Refresh Token，也不是账号密码。

Agent 读取目录时，通过它所在环境的私密凭据机制传入 `GEMINI_AGENT_GATEWAY_KEY`，不要打印值：

```sh
python3 scripts/agent_kit.py doctor --base-url http://127.0.0.1:8787/v1
python3 scripts/agent_kit.py models --base-url http://127.0.0.1:8787/v1
```

已使用 Magpie 或 Antigravity Manager 的用户可保留原网关，换成其实际地址和对应凭据即可。后面的示例统一采用面板地址；不是要求重装已正常工作的服务。所有 `127.0.0.1` 示例都要求调用者与网关处于同一台机器或同一运行环境。

## 选择接入方式

| Agent | 优先检查的入口 | 完成后是什么效果 |
| --- | --- | --- |
| Codex 桌面 | 面板的可选 Codex 配置；见下文 | 新增可选择的 Gemini 来源 |
| Codex CLI | `launch-codex` | 仅这次启动的 CLI 会话使用指定来源 |
| WorkBuddy | 当前版本的自定义模型界面 | 选中后由网关模型承担该任务的模型调用 |
| Muse | 当前产品的 MCP / 命令连接器；或已授权设备文件工具 | Gemini 作为外援完成明确子任务 |
| GrokBot / Grok Bot | 自己的虚拟机、命令或 MCP 工具 | 在该运行环境调用 Gemini 并收回结果 |
| 豆包工作 | 先检查当前版本是否开放自定义模型、MCP、HTTP 或本机文件工具 | 按实际接口选择直连或外援；本仓库尚未完成其产品级验收 |
| DSH NEXT | 自定义 OpenAI Chat Completions 来源 | 新增模型后，在目标会话中选择 |
| 其他 Agent | 自定义 OpenAI API，或 stdio MCP | 使用同一套 API 或 `ask_gemini` 工具 |

只有自定义模型入口时可以新增来源，但未经用户要求不切换原默认；只有工具入口时走外援。**配置保存、网关有回复、客户端完成任务**分别记录；支持某种协议不等于所有产品版本已实测通过。

## Codex

### 桌面客户端

用户明确要连接 Codex 时，打开面板高级选项里的 Codex 配置：选择真实型号，生成配置预览，再确认添加。面板会私有备份原配置，增加独立来源，保留现有默认模型。恢复也走面板的恢复按钮，不覆盖用户后续更改。

使用时保持网关运行。若要重启 Codex，先把面板放在独立终端或用户级后台服务中，不能依赖即将退出的 Codex 会话。重启后按当前客户端支持的来源选择入口启用 `Gemini Agent Kit`，再开新会话选择型号。若执行 Agent 无法操作自身窗口，只交接这一步给用户，其他可完成的准备先做好。详细操作见 [本机面板](LOCAL_PANEL.md)。

### CLI：只影响本次启动

```sh
python3 scripts/agent_kit.py launch-codex --base-url http://127.0.0.1:8787/v1 --model "完整模型 ID"
```

启动器设置独立 Responses provider，把网关密钥通过子进程环境传入，不写全局 `config.toml`。退出该进程即可结束这次配置。额外参数放在 `--` 后，例如：

```sh
python3 scripts/agent_kit.py launch-codex --base-url http://127.0.0.1:8787/v1 --model "完整模型 ID" -- exec "读取 brief.txt，把三个不同开头保存为 draft.txt"
```

`--dry-run` 只生成启动参数，不证明模型调用成功。CLI 成功也不代表桌面旧会话已切换。

## WorkBuddy

在当前应用的自定义模型界面添加独立来源，填上面的完整 Chat Completions URL、真实模型 ID 和网关 API Key。若界面要求的是 Base URL，则只填到 `/v1`。按来源能力启用工具调用，在新任务中实际选中该模型。先备份，只添加所需项目，不覆盖整份配置。

需要配置计划时：

```sh
python3 scripts/agent_kit.py plan --client workbuddy --base-url http://127.0.0.1:8787/v1 --model "完整模型 ID" --output reports/workbuddy-plan-01.json
```

计划文件不含密钥，也不会代替点击保存。使用已有 Magpie 的用户也可沿用其 WorkBuddy adapter。若希望保留主模型，且 WorkBuddy 有已授权的本机文件工具，可以采用下面的文件桥外援路线。

## Muse、GrokBot、豆包工作及其他工具型 Agent

支持相应连接器时，可用 `plan --client muse`、`grokbot`、`doubao` 或 `generic` 生成标准工具计划；选择器名称不代表已验证该产品所有版本。生成后按当前产品实际支持的入口配置：

```sh
python3 scripts/agent_kit.py plan --client generic --base-url http://127.0.0.1:8787/v1 --model "完整模型 ID" --output reports/generic-plan-01.json
```

### 能执行命令：直接交一个任务

不必先装 MCP。让 Agent 把完整任务写进 UTF-8 的 `task.txt`，然后执行：

```sh
python3 scripts/agent_kit.py ask --base-url http://127.0.0.1:8787/v1 --model "完整模型 ID" --prompt-file task.txt --output reports/reply-01.json --max-tokens 1024
```

网关密钥通过私密环境变量 `GEMINI_AGENT_GATEWAY_KEY` 注入。命令只发起一次文字请求，不自动重试；结果保存为脱敏 JSON，包含 `text`、请求型号、上游自报型号及用量。终端只报告输出路径，Agent 再读取 `text` 完成后续任务。输出文件必须使用新名字，已有文件不会被覆盖。这里 Gemini 处理的是明确子任务，宿主仍负责读文件、执行工具和最终交付。

### 有 stdio MCP 连接器

在**连接器实际执行的环境**放置仓库，配置本仓库的 `ask_gemini`。以下为常见 `mcpServers` 格式；具体产品使用自己的配置界面或格式：

```json
{
  "mcpServers": {
    "gemini-agent-kit": {
      "command": "python3",
      "args": [
        "/absolute/path/to/gemini-agent-kit/scripts/agent_kit.py",
        "mcp",
        "--base-url", "http://127.0.0.1:8787/v1",
        "--model", "目录中的完整模型 ID"
      ]
    }
  }
}
```

将脚本路径、解释器、型号替换为该环境的实际值；通过连接器的私密环境设置注入 `GEMINI_AGENT_GATEWAY_KEY`。连接后应能发现 `ask_gemini`，参数为 `prompt` 和可选 `max_tokens`。宿主读取材料、组织任务，把明确子任务交给 Gemini，再使用返回内容完成文件或答复。

只有 HTTP 远程 MCP 的产品不能直接运行这条 stdio 命令。本仓库没有 HTTP MCP 服务；不要把 OpenAI API URL 填进远程 MCP 地址框。

### 有设备文件工具，但没有本机 shell 或 MCP

在 macOS / Linux 本机运行 [文件桥 worker](FILE_BRIDGE.md)，授权宿主访问专用队列。宿主写请求 JSON，worker 调用网关，宿主读结果并完成任务。启动 worker 需要本机执行能力；如果当前 Agent 没有，就交给用户本机另一个能执行命令的 Agent，不要声称云端已经启动成功。

这条路线在历史 Muse 与 WorkBuddy 环境使用过，不代表所有同名产品均有设备文件权限。它保留宿主主模型；文件桥默认也不增加固定等待；只有明确需要时才设置额外间隔，不要再叠加排队代理。

### GrokBot 有自己的虚拟机

优先复用虚拟机中已有的、用户授权的 Pi / Magpie 或兼容网关；网关与调用工具在同一虚拟机时，可用该虚拟机的回环地址。没有来源时，使用已有鉴权 HTTPS 网关或交接本机准备，不向云端 Agent 提供主账号长期 Refresh Token。先确认虚拟机内可调用，再让 GrokBot 聊天实际发起并收取子任务。前者不能替代后者，外层 GrokBot 的额度也不因增加 Gemini 工具而改变。

### 豆包工作与其他尚未验收的产品

让执行 Agent 先查看自己实际可用的工具及当前产品设置：

- 有自定义 OpenAI 模型入口：使用本页三个值配置独立来源。
- 有命令执行工具：使用上面的 `ask` 单次命令；有 stdio MCP 则配置 `ask_gemini`。
- 有已授权的设备文件工具：由本机启动文件桥后，按队列调用。
- 只有聊天或远程 MCP，且无法访问已有鉴权网关：先把本机部署交给有执行能力的 Agent。说明还缺哪个入口，不虚构产品按钮或连接成功。

仓库提供通用协议和执行指引，不能替一个封闭产品增加它没有开放的接口。豆包工作的具体版本支持情况和真实任务结果需由使用者所在环境确认。

## DSH NEXT

在 **设置 → 模型** 添加独立 OpenAI Chat Completions 来源。按 DSH `2.0.22-next` 的配置约定，API 类型为 `openai-completions`，Base URL 填 `http://127.0.0.1:8787/v1`；模型用实时目录完整 ID。网关密钥保存于本机私有 credentials，来源引用对应凭据，保留原模型与设置。

保存后在目标会话的模型菜单选中它。该版本选择模型会更新全局默认，事先记录原默认，用户要求临时测试时在结束后切回。不要把某个示例型号或原来的默认模型写死到其他人的配置中。

## 云端与本机的位置

云端 Agent 的 `localhost` 指向云端自身，不能直接访问用户电脑的 `127.0.0.1:8787`。优先复用用户已有的鉴权 HTTPS 网关，或明确支持的设备文件桥；没有可用来源就交接本机准备。账号由本人在自己管理的电脑录入，云端只接收必要的网关 API Key，不接收主账号长期 Refresh Token。不要为了演示直接暴露本机管理面板到公网。

## 历史记录与版本差异

以下是特定环境的历史测试，**不是新用户安装步骤，也不是当前产品能力保证**。完整次数、失败原因与产物范围见 [STATUS](STATUS.md)。

- WorkBuddy 的 Hy4 宿主曾通过文件桥调用 Gemini 一次：HTTP 200，94.759 秒；宿主保留原稿，删除两段材料没有提供的活动安排，得到 192 字正文。主模型直连的另一轮返回 429，不能把外援通过算成主模型替换通过。
- Muse 设备文件路线曾产生写稿、订单整理成果；宿主仍负责调度与文件落地。Magpie 的 `muse` adapter 是 **Meta Muse Code CLI**，不是所有 Muse 桌面或网页产品，不应据此修改另一款应用。
- DSH 一轮短测界面与路由已确认，但收到 `429 RESOURCE_EXHAUSTED`，完成 0/1；并无可确认的恢复时间。
- GrokBot 历史虚拟机两次 Pi 写稿均返回 429，完成 0/2；短探针 HTTP 200 但未满足内容要求，外层聊天当时额度耗尽。用户之后自行修复的配置不在这轮记录内，不能用旧结果判断其现在不可用。

新部署以新用户的实际来源、型号、任务和结果为准。面板默认不添加固定请求间隔；上游限流仍按真实提示处理，不能把一次短测写成无限额度或长期稳定证明。
