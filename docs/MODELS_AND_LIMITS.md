# 模型、登录与额度

模型能力、账号可用性和协议兼容性需要分别确认。Magpie 把已有来源提供给客户端；它不为账号创造额外模型权限或配额。

## 选择实际可用的型号

运行 `models`，复制目录中的完整 `id`。同一型号可能来自不同 provider，不能只写一个裸名字。仓库不固定发布一份“所有账号可用模型表”。

CLI 仅在目录中恰好有一个同时包含 Gemini 与 Argon 的条目时自动选择；否则要求 `--model`。指定 ID 不在目录中会报错，不会切换到 Carbon 或其他型号。

Pro、Ultra 是订阅层级，Argon、Carbon 是模型标识。2026-11-15 核对的 Antigravity 官方型号权限如下：

| 型号 | Google AI Pro | Google AI Ultra |
| --- | --- | --- |
| Gemini 4 Argon | 付费 Pro 可用；部分符合条件的高级设备试用也可用 | 可用 |
| Gemini 4 Carbon | 不可用 | 可用 |
| Gemini 4 Hydrogen | 可用 | 可用 |

这张表限定于 Antigravity 产品入口，不直接赋予第三方 API 访问权。其他来源仍须以当前权限、动态目录和真实请求为准。目录列出某型号也可能只是可选列表，真实请求仍可能因权限、额度或地区失败。[Antigravity 型号权限表](https://antigravity.google/docs/models)

核对来源：[Google Antigravity Plans](https://antigravity.google/docs/plans)、[Gemini 模型文档](https://ai.google.dev/gemini-api/docs/models)。

## Magpie 中的 Gemini 来源

可以复用用户已经配置的来源，也可以在 Magpie 的 Providers 界面连接其自有可用来源。仓库不要求每个人新建 Google AI Studio 项目。

但“Google 登录”不是一种通用权限。核查于 2026-11-15，Magpie 的专门订阅文档将 Gemini 登录列为 Code Assist Standard / Enterprise 路线，并要求自有 Cloud 项目。它还把直接 Antigravity 登录标为退役，介绍了社区 local agy CLI 插件。不要将普通 Pro 订阅与这一 Code Assist 路线视作同一产品。[Magpie 订阅说明](https://usemagpie.ai/docs/subscriptions)

Google FAQ 明确不支持消费者计划将 `agy` 包装进第三方 coding agent，并说明第三方访问 Antigravity 的账号风险。存在社区插件不代表 Google 官方支持，也不代表本仓库已对该来源做完实测。[Google Antigravity FAQ](https://antigravity.google/docs/faq)

文档和产品会更新。执行 Agent 应核对当前版本；不要套用旧教程导入账号令牌，也不要把已有兼容网关统一判定成必须换用官方项目 API。

## 三种“密钥”不要混用

| 内容 | 用途 |
| --- | --- |
| 本机 Magpie 客户端标签 | 区分哪个客户端发起请求，不是上游账号密钥 |
| 远程网关密钥 | 通过网关鉴权，由网关所有者管理 |
| 模型来源的 API Key / 登录凭据 | 由来源及 Magpie 的正常配置流程管理，不进入公开仓库 |

CLI 使用 `GEMINI_AGENT_GATEWAY_KEY` 读取网关凭据。本机 Magpie 没有指定该变量时使用客户端标签。其他本机服务是否接受该标签，由服务自己决定；若需鉴权，必须提供其有效网关密钥。

## 怎样描述额度与成功率

官方把 Pro 的额度描述为较高、充裕，按五小时刷新，但受每周上限约束；Ultra 的基础额度更高，也有周期限制。这可以支持“额度充裕”的介绍，不能改写成“无限 API”。[Antigravity 额度说明](https://antigravity.google/docs/plans)

额度还与来源、型号、上下文长度、任务复杂度、并发和刷新周期有关。以供应商当前规则和账户实际显示为准，不承诺固定每天请求数或某种渠道账号永久可用。

`verify` 一轮最多发出 3 次短请求。通过只能说明这一网关在这些输入下完成了文字与工具往返。它不测所有模型能力、不证明长期稳定性，也不独立鉴定上游模型来源。

若使用路由组，网关还可能执行降级。比较模型或录制演示时，优先选明确来源与型号，保存请求 ID、网关自报型号、usage 和实际输出；自报型号应按自报信息呈现。

任何公开演示应标注样本数和测试范围，不把模拟服务测试、列表可见、连接器已保存当作真实 Gemini 端到端成功。
