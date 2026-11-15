# Gemini Agent Kit

**把这个仓库交给你的 Agent，让它把已有的 Gemini 模型源接进你常用的工作流。**

默认复用 Magpie：发现网关、读取真实模型目录、连接客户端，再检查文字回复和工具调用。支持 Codex CLI、WorkBuddy，以及能运行命令或 MCP 的 Muse / Grok Bot 工作流。

> 请读取 https://github.com/zysen1/gemini-agent-kit 的 AGENTS.md，直接在我的环境中完成接入：优先复用已有 Magpie；未安装则安装官方版本并启动；动态读取模型目录，选择我可用的 Gemini 模型；连接我已安装的 Agent，完成真实任务验收并保存脱敏结果。需要我登录时只让我在官方界面完成，其他可执行步骤请继续完成。

[完整交给 Agent 的提示词](PROMPT.md) · [各客户端接法](docs/CLIENTS.md) · [模型与额度](docs/MODELS_AND_LIMITS.md) · [验证范围](docs/STATUS.md)

## 快速开始

需要 Python 3.10+；脚本只用标准库。Windows 可将 `python3` 换成 `py -3`。

```sh
git clone https://github.com/zysen1/gemini-agent-kit.git
cd gemini-agent-kit
python3 scripts/agent_kit.py doctor
python3 scripts/agent_kit.py models
```

如果 Magpie 尚未运行，先打开 [Magpie 官方应用](https://usemagpie.ai/)，或启动已安装的 `magpie serve`。首次使用需要在 Magpie 的 Providers 页面连接你自己的可用模型源。已有来源可直接复用，不要求重新申请官方项目 API。

从 `models` 输出复制完整 `id`，替换下方占位内容：

```sh
python3 scripts/agent_kit.py verify --model "完整模型 ID" --output reports/gateway-check-01.json
python3 scripts/agent_kit.py launch-codex --model "完整模型 ID"
```

`verify` 会发出最多 3 次短请求，检查文字回复、函数参数和工具结果回传，消耗所选来源的额度。它通过后，仍要在目标客户端完成一个真实任务；它不测 Codex 的 Responses 接口，也不代表所有客户端均已跑通。

若目录中恰好只有一个 Gemini Argon 条目，可省略 `--model`。存在多个来源或其他型号时必须指定，程序不会偷偷换模型。

## 其他客户端

```sh
python3 scripts/agent_kit.py plan --client workbuddy --model "完整模型 ID" --output reports/workbuddy-plan-01.json
python3 scripts/agent_kit.py plan --client muse --model "完整模型 ID" --output reports/muse-plan-01.json
python3 scripts/agent_kit.py plan --client grokbot --model "完整模型 ID" --output reports/grokbot-plan-01.json
```

`plan` 生成可核对的配置，不会替你点击客户端。执行 Agent 应继续按 [AGENTS.md](AGENTS.md) 完成配置、真实任务和验收。Codex 启动器只影响当前 CLI 进程；WorkBuddy 用自定义模型；Muse / Grok Bot 的通用路线是 `ask_gemini` 外部工具，原 Agent 继续规划任务。

## 网关在哪里

默认地址是 `http://127.0.0.1:3425/v1`。修改端口或使用其他兼容网关时，给命令加 `--base-url`。例如：

```sh
python3 scripts/agent_kit.py doctor --base-url http://127.0.0.1:8080/v1
```

本机 Magpie 使用客户端标签即可。其他网关若需要密钥，在执行环境中私下设置 `GEMINI_AGENT_GATEWAY_KEY`；程序不会读取 `.env` 文件。远程网关默认要求 HTTPS 和该环境变量。别把上游账号密码或 Refresh Token 填进这个变量。

云端 Agent 里的 `127.0.0.1` 是云端自己的电脑。应在其运行环境中部署网关，或使用已有的、带鉴权的 HTTPS 网关；本项目不会自动公开你的电脑。

## 这个仓库负责什么

| 能力 | 范围 |
| --- | --- |
| `doctor` / `models` | 检查环境和网关，读取动态目录，不发推理请求 |
| `verify` | 真实网关的短文本与工具往返检查，保存脱敏 JSON |
| `launch-codex` | 用独立 provider 启动 Codex CLI，不改全局配置 |
| `plan` | 生成客户端配置说明，不把“计划生成”算作“配置完成” |
| `mcp` | 提供 stdio MCP 工具 `ask_gemini`，把指定文字任务交给所选模型 |

Magpie 负责来源连接和协议转换；本仓库负责发现、配置辅助和验收。仓库本身没有账号、订阅或模型额度，也不承诺某种消费者订阅必然能转换成 API。关于 Google 登录路径与型号权限，见 [模型与额度说明](docs/MODELS_AND_LIMITS.md)。

## 开发验证

```sh
python3 -m unittest discover -s tests -v
```

这些测试使用本地模拟服务验证程序行为，不会调用真实 Gemini。测试通过与真实模型、客户端端到端成功是不同的证据，见 [STATUS](docs/STATUS.md)。
