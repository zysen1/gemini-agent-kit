# Antigravity Manager 独立接入路线

已有 Antigravity Manager，或用户明确选择它时，可以直接接本仓库，**不必经过 Magpie，也不要求 Google AI Studio 项目密钥**。

```text
用户授权的账号 → Antigravity Manager → 本地兼容 API → 客户端
```

这里的 Manager 是 [lbjlaq/Antigravity-Manager](https://github.com/lbjlaq/Antigravity-Manager)，应用名为 Antigravity Tools，是第三方项目。本页说明用户自行管理的独立 Manager，并保留 `v5.0.0` 源码依据及 `v5.0.3` 历史验收记录；新手默认安装请用[本机面板](LOCAL_PANEL.md)的固定版本流程。自行管理的应用可能自动升级，操作时以实际运行版本为准；不要将本页历史流程当成 Kit 自动安装器的行为。

## 1. 安装项目发布版

从[项目 Releases](https://github.com/lbjlaq/Antigravity-Manager/releases)选择对应系统和架构。用户指定 `v5.0.0` 时使用[该版本发布页](https://github.com/lbjlaq/Antigravity-Manager/releases/tag/v5.0.0)，不要自行改成开发分支。

- Apple Silicon：`Antigravity.Tools_5.0.0_aarch64.dmg`。
- Intel Mac：选择该版本的 x64 包。
- Windows / Linux：选择当前系统对应的发布包，按项目安装说明安装。

核对项目来源及可用的签名／校验信息，不为完成安装关闭系统安全功能。用户已经运行并授权的网关可按实际地址复用；仅版本相符不能证明 `.app` 可信，也不授权 Agent 扫描并执行未验证的应用。Kit 的 Mac / Linux 自动准备流程使用固定摘要发布包及缓存完整性记录，Windows 仍为用户手工安装。

### 记录运行版本，别只看安装包名称

v5.0.0 的更新组件在检测到更新后会调用 `downloadAndInstall`，完成后再显示重启提示。因此可能出现磁盘上的 `.app` 已更新、正在提供 API 的旧进程仍未重启的情况。选择过 v5.0.0 安装包，并不能证明后续运行的一直是 v5.0.0。[更新组件源码](https://github.com/lbjlaq/Antigravity-Manager/blob/v5.0.0/src/components/UpdateNotification.tsx)

每轮验收记录请求时间与 `GET /v1/version` 返回的运行版本；另外记录应用“关于”页或磁盘包版本，两者不一致时分别注明。先结束当前验收再正常重启，并重新检查版本和接口；不要把重启前后的结果混成同一版本的成功率。也不要为了维持旧版而关闭系统安全检查。

2026-11-15 本机先运行 `5.0.0`，21:07 在结束客户端任务、停止排队代理并私密备份账号目录后，正常重启到磁盘上的项目发布版 `5.0.3`。`GET /v1/version` 已返回 `5.0.3`，仍使用同一隔离目录、同一个 PRO 账号和同一网关密钥，没有清除冷却或增加账号。升级自动加入了三条默认 Claude 映射；它们不匹配本次请求的 `gemini-4-argon-high`。这些是本次环境的实测记录，不是每次升级的保证。

源码对照显示，`v5.0.0` 的流式首选端点有约 30 秒起步的响应看门狗（较大请求可放宽到 45 秒），备用端点上限 25 秒；`v5.0.3` 改用上传和首字等待分阶段预算，并按配置与估计输入量计算首字等待。旧版这条固定短等待可能影响高思考或较长输入，因此正常发布版升级值得复验；它不能增加 Google 配额。升级后的 WorkBuddy 本轮仍收到上游 `429 / RESOURCE_EXHAUSTED`，具体配额桶未得到明确字段，不能宣称超时改进解决了额度问题。参见 [`v5.0.0 client.rs`](https://github.com/lbjlaq/Antigravity-Manager/blob/v5.0.0/src-tauri/src/proxy/upstream/client.rs) 与 [`v5.0.3 client.rs`](https://github.com/lbjlaq/Antigravity-Manager/blob/v5.0.3/src-tauri/src/proxy/upstream/client.rs)。

## 2. 单账号验收使用隔离目录

Manager 默认数据目录是 `~/.antigravity_tools`，其中包含私有账号资料。环境变量 `ABV_DATA_DIR` 可指定独立目录；在**应用新进程启动之前**设置。

以下 Mac 示例仅适用于用户已核实来源、明确选择的自管应用，不是 Kit 的默认安装路线；应用实际路径不同则调整 `manager_app`：

```sh
manager_app="/Applications/Antigravity Tools.app"
manager_data_dir="$HOME/.local/share/gemini-agent-kit/manager"
mkdir -p "$manager_data_dir"
chmod 700 "$manager_data_dir"
manager_executable=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$manager_app/Contents/Info.plist")
ABV_DATA_DIR="$manager_data_dir" "$manager_app/Contents/MacOS/$manager_executable"
```

Manager 使用单实例机制。已有应用正在运行时，应等现有任务结束后正常退出，再从新环境启动；唤起旧窗口不会使新的隔离环境变量生效。不要终止用户正在使用的会话。Windows / Linux 同样应在启动应用的进程环境中设置 `ABV_DATA_DIR`。

之后固定使用这条启动路线，避免下次从图标打开时回到原数据目录。私有目录不放在 Git 仓库内，不上传账号文件；也不要改变系统 `HOME`。

本次只导入一个授权账号。v5.0.0 的“固定账号”实际是优先账号，限流或额度耗尽时仍可能切到其他账号。隔离的单账号池能避免把其他账号的成功混进验收。已有多账号环境必须保留时，记录原状态后用反代启用开关暂时停用其他账号，验收后恢复，不删除账号。

## 3. 在应用原生界面导入

由账号本人在自己管理的电脑录入其授权账号；不向 Agent 聊天提供 Refresh Token，也不向云端虚拟机导入主账号长期 Token。已有登录网关无需重新导入或索要 Google AI Studio Key。

1. 打开 **账号管理 → 添加账号 → Refresh Token**。
2. 用户在该本机界面直接粘贴，不把 Token 发到聊天或临时文件。
3. 点确认添加，等待应用读取身份和额度。导入后遮挡账号标识再采集截图。
4. 检查账号状态、订阅层级和模型目录；“已添加”不等于模型推理已经通过。

v5.0.0 的输入框支持单个 Token、带 `refresh_token` 字段的 JSON 数组，也会从粘贴文本中识别 Token。单账号测试只输入此次授权账号。不要从其他应用或浏览器提取 Token，不使用数据库导入来替代用户授权。

项目源码参考：[导入界面](https://github.com/lbjlaq/Antigravity-Manager/blob/v5.0.0/src/components/accounts/AddAccountDialog.tsx)、[添加账号流程](https://github.com/lbjlaq/Antigravity-Manager/blob/v5.0.0/src-tauri/src/modules/account_service.rs)。

## 4. 启用反代并选真实型号

在 Manager 的 API 反代页面：

1. 保留本机访问，通常是 `127.0.0.1:8045`；端口被占用就选空闲端口并记录。
2. 启用“仅暴露真实配额模型”对应选项（`only_raw_quota_models`），避免把内置 GPT／Claude 别名当成真实型号。
3. 检查模型路由：单型号验收不添加将 Gemini 指向其他模型的自定义映射。
4. 启动反代服务，确认实际地址、鉴权方式与 API 网关密钥。v5.0.0 首次启动会持久化反代自动启动设置；结束时按用户需要保留或恢复。

端口和鉴权以当前应用配置为准。默认 `auto` 在仅回环模式下可免代理鉴权，但管理接口仍有独立鉴权。推荐给客户端填写 Manager 页面提供的**网关密钥**，不要把测试依赖在免鉴权行为上。

| 凭据 | 只用于 |
| --- | --- |
| Google Refresh Token | Manager 的账号导入／上游登录续期 |
| Manager API 网关密钥 | Codex、WorkBuddy、Kit 等客户端访问网关 |
| Manager 管理密码 | 管理后台；是否单独设置由当前配置决定 |

将网关密钥通过本机私密输入或连接器安全凭据配置传入 `GEMINI_AGENT_GATEWAY_KEY`。不要把真实值放进示例命令、命令历史、公开报告或 Git。程序不自动读取 `.env`。Google Refresh Token **不能**放进这个变量或客户端 API Key 栏。

## 5. 接入 Kit 并做一次短测

以下命令在克隆的仓库目录内执行。已安全配置网关密钥后：

```sh
python3 scripts/agent_kit.py doctor --base-url http://127.0.0.1:8045/v1
python3 scripts/agent_kit.py models --base-url http://127.0.0.1:8045/v1
```

Manager 没有 Magpie 的 `/api/hello`；`doctor` 中该项不可用属于预期，关键是模型目录成功读取。Manager 自身有 `/v1/version`。不要因为 `magpie_hello` 不可用而要求换网关。

从目录复制确切模型 ID。Argon 可能同时有裸型号和 `high`、`medium`、`low` 档位，因此建议显式选择实际存在的档位；不要假设示例名在每个账号下都可用。

```sh
python3 scripts/agent_kit.py verify --base-url http://127.0.0.1:8045/v1 --model "目录中的完整 Argon ID" --timeout 120 --output reports/manager-check-01.json
```

这一步最多发送 3 次短请求，检查文字输出、工具调用参数和工具结果回传。输出文件必须是新文件名。通过后逐个验证客户端，不要同时把四个客户端都压到同一个账号。

## 6. 连接客户端

### Codex CLI

```sh
python3 scripts/agent_kit.py launch-codex --base-url http://127.0.0.1:8045/v1 --model "目录中的完整 Argon ID"
```

Manager 实现 `/v1/responses`；Kit 使用独立 provider 启动当前 Codex CLI 进程，不改全局配置。让它读取短材料并生成文件，再检查产物；Chat 短测通过不能代替这一步。

### WorkBuddy

```sh
python3 scripts/agent_kit.py plan --client workbuddy --base-url http://127.0.0.1:8045/v1 --model "目录中的完整 Argon ID" --output reports/manager-workbuddy-01.json
```

在 WorkBuddy 自定义模型中填写完整 API URL `http://127.0.0.1:8045/v1/chat/completions`、目录中的模型 ID 和 **Manager 网关密钥**。保存后在新任务中选中该模型；只保存表单不算端到端成功。

若任务还没发请求就报 `ENOENT`，先检查工作目录是否存在、进程是否能访问。可在实际存在、简单英文路径的临时目录复验，区分文件路径问题与模型问题；不要据此声称所有中文目录都不支持。

### Muse / Grok Bot

按[客户端指南](CLIENTS.md)建立命令或 stdio MCP 连接器，把同样的网关地址、精确模型 ID 和私密网关凭据传入。通用路线仍是 `ask_gemini` 外部工具，不能据此宣称已替换产品主模型。

云端 Agent 不能访问用户 Mac 的 `127.0.0.1`。优先复用已有授权网关，没有来源则交接本机准备；不要向云端 Agent 提供主账号长期 Token，也不要为了连通直接开放 Manager 管理端口。此仓库不自带 HTTP MCP 服务。

## 7. 单账号限流按冷却处理

- 保持串行：网关短测完成后，再依次测试客户端。
- 遇到 `429` 或包含明确限流原因的 `503`，读取 `Retry-After`、错误中的 `retryDelay` 或 Manager 显示的剩余冷却时间，等到冷却结束再进行一次短重试。
- 没有冷却信息时先检查服务状态和额度，不持续轮询模型请求。`503` 也可能是上游不可用、无可调度账号等原因，应记录实际错误。
- 不清除限流记录来制造“成功”，不临时加账号轮换，不把连续重试算成独立稳定性样本。

记录成功次数、总请求数、首次失败与恢复时间。单次三请求短测通过可以说明该次文字与工具链路有效，不能证明长期成功率或无限配额。

## 8. 型号身份和验证边界

`only_raw_quota_models=true` 只收窄目录展示，不会关闭所有后端路由。v5.0.0 仍有型号别名、旧模型转发及档位解析；“固定账号”也不是严格禁用故障转移的总开关。

验收时保留精确请求型号，并在可取得时记录响应 `model`、`X-Mapped-Model` 和 `X-Upstream-Model`。Kit 当前 JSON 报告会记录响应的 `model`，**不会自动记录这两个 HTTP 头**；可由独立脱敏请求记录或 Manager 日志补充。不要为了采集这两项导出包含凭据的完整请求日志。

这些是网关／上游自报信息，不是独立模型鉴定。型号不符应单独报告，不能把它算作指定型号通过。每个客户端的实际任务仍需单独验收，本指南不宣称四端已经全部成功。

Google 对消费者计划第三方访问的支持边界另见 [Antigravity FAQ](https://antigravity.google/docs/faq)。技术接通、账号订阅和官方支持是不同事项；这条独立路线不要求用户先换成 AI Studio Key，也不意味着所有账号都具备同样的权限。

## 恢复与排障

退出 Kit 启动的 Codex 会话即可结束该进程配置。WorkBuddy／连接器恢复原模型或移除本次新增项；Manager 按验收前记录恢复自启动和账号反代状态。隔离目录仍含私有登录资料，交付时告知位置，未经用户要求不要删除或上传。

Manager 的导入、目录与上游错误向[Manager 项目](https://github.com/lbjlaq/Antigravity-Manager/issues)核对；Kit 的参数、报告或 MCP 行为向[本仓库](https://github.com/zysen1/gemini-agent-kit/issues)反馈。公开 issue 只附版本、脱敏错误、接入方式和可复现步骤，不附账号 Token 或网关密钥。
