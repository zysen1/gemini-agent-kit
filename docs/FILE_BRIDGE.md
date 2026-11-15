# Muse 等云端 Agent 的本机文件桥接

本桥当前在 macOS／Linux 上运行，使用 POSIX 目录描述符固定文件边界；Windows 上的其他仓库命令不受影响。

如果 Agent 已有用户授权的 **Mac 文件读写**，但没有本机 shell、HTTP 或 stdio MCP，可使用这条备选路线。它依赖该产品实际提供的设备文件工具；不能把这份文档当作所有版本都支持的保证。

流程：Agent 写入一份请求 JSON → Mac 上的 worker 调用本机 Gemini 网关 → Agent 读取结果 JSON → 使用真实返回内容完成任务。网关继续只监听本机回环地址，Google Token 与网关密钥都不传到云端，没有新增公开端口。

这是**文件中继形式的外部模型工具**，不替换 Muse 的主模型，也不是远程 HTTP MCP 服务。文件可写只证明设备连接；必须看到 `success: true` 和实际成果才能记录本轮调用成功。

## 在 Mac 启动

先按 [Manager 指南](ANTIGRAVITY_MANAGER.md) 登录、启动网关并完成一次真实验收。从当前模型目录复制精确 ID。网关凭据通过执行环境 `GEMINI_AGENT_GATEWAY_KEY` 注入；不要写进命令参数、聊天、请求 JSON、截图或 Git。

在仓库目录执行（示例目录为独立英文路径）：

```sh
python3 scripts/file_bridge.py \
  --queue /Users/yourname/gemini-muse-relay \
  --receipt-dir /Users/yourname/Library/Application\ Support/GeminiBridge/private-receipts \
  --base-url http://127.0.0.1:8045/v1 \
  --model gemini-4-argon-high
```

将 `/Users/yourname` 换成真实用户目录。`--receipt-dir` 必须在共享队列之外，不授权云端 Agent 读取或写入；这里存放本机回执、ID 占用记录、冷却和进程锁。worker 会创建 `requests`、`results`、`processed`，终端输出 `worker: ready`。同一队列只运行一个 worker；默认请求结束后至少间隔 35 秒。

脚本仅接受回环网关，要求非空网关密钥；它不会读取任何产品凭据或替你登录。鉴权请求禁止重定向，也不经过环境 HTTP 代理。

## 给云端 Agent 的操作规范

只授权它读写专用队列的 `requests`、`results`。不要授权私有回执目录。每次使用新的 ID（1–64 个英文字母、数字、下划线或连字符，以字母或数字开头）。例如写入：

`/Users/yourname/gemini-muse-relay/requests/muse-first-writing.json`

```json
{
  "id": "muse-first-writing",
  "prompt": "请依据以下虚构活动材料写三种开头和一段口播。只使用材料中的事实……",
  "max_tokens": 2500
}
```

只有这三个字段；不得传入 URL、模型、输出路径、命令或凭据。`prompt` 最多 16,000 字符，`max_tokens` 为 1–4096 的整数，整个文件最多 64 KiB。完整写入一份 JSON；设备工具支持时，先写临时文件再原子重命名为 `.json`。worker 会等待文件稳定，未完成的 JSON 不会触发推理。

随后通过该产品的设备文件工具读取：

`/Users/yourname/gemini-muse-relay/results/muse-first-writing.json`

文件尚不存在时按 5–10 秒间隔检查即可，不要重复提交。单次 HTTP 超时默认 180 秒，可由启动者用 `--timeout` 在 1–600 秒内调整。

结果关键字段：

- `success`、`text`：调用是否成功及真实返回文字。
- `http_status`、`error`、`detail`：失败时的实际状态或脱敏原因。
- `requested_model`、`provider_reported_model`、`route_headers`：请求 ID、网关自报 ID、`X-Mapped-Model`／`X-Upstream-Model`；缺失头为 `null`。
- `usage`、`finish_reason`、时间戳、耗时：网关返回的用量及本机执行记录。
- `inference_request_count`：本请求实际尝试 HTTP 推理的次数，通常 1；输入被拒绝时为 0。
- `model_identity_independently_verified: false`：自报型号与路由头不是独立来源鉴定。

共享结果可以被有写权限的宿主改写，因此 **不能单独充当真实调用证明**。本机检查者需要对照私有回执中的同一 `id`、`receipt_id`、请求 SHA-256、HTTP 状态及结果规范 JSON 哈希（UTF-8、`ensure_ascii=False, sort_keys=True, separators=(",", ":")`）。无私有回执，或哈希不一致，不算成功证据。回执记录本机 worker 收到的 HTTP 结果，仍不独立证明网关背后的模型身份。

“位于队列之外”或文件权限 `700` 不能阻止一个已获得同用户全盘文件权限的宿主。应由产品的设备访问权限限制其目录范围；如果产品做不到，这份回执只是另一路本机记录，不是防篡改证明，需由本机操作者交叉核对网关日志和实际产物。

Agent 必须使用 `text` 完成后续产物，并保留请求、结果及对应文件记录。只由宿主自己写稿不能声称 Gemini 完成。换输入复验时重新读取材料，使用全新 ID，并核对新的 nonce 或事实。

## 冷却、重启与清理

遇到 429／503，worker **不自动重试、不换型号、不换账号**。它记录失败，并尊重 `Retry-After` 或错误中 `Cooldown wait …s` 等时间；该冷却也约束队列后续请求，并写入私有回执目录的 `.cooldown.json`，重启不会清空等待。没有明确冷却头时仍至少等待设定间隔。不要删除冷却状态来规避服务限制。

同一 ID 的已有结果永不覆盖，私有 `.claim-<id>.json` 使已占用 ID 不会再次调用；删除共享 `results` 或 `processed` 不会重放该 ID。`--once` 最多处理一个完整且已到冷却时间的请求后退出；退出码 2 表示本次没有可处理请求，不表示模型成功。常驻模式用 Ctrl+C 停止。

私有回执目录的 `.worker.lock` 记录 PID，防止误启动多个 worker。异常退出后，先确认记录的进程确已停止，才可删除这个锁文件。已经移入 `processed`、却没有对应结果的请求可能在崩溃前已调用；先查文字日志再决定是否用新 ID 重试，不应默默重放。

请求和结果是任务材料，也可能含非公开内容。只将经过核对的成功执行画面与成果放进视频素材；不要打包队列、凭据或运行目录到公开仓库。撤销接入时停止 worker，并撤销云端 Agent 对专用目录的访问；保留验收记录或按用户要求清理。

## 离线测试

```sh
python3 -m unittest discover -s tests -p test_file_bridge.py -v
```

测试覆盖输入字段和大小限制、符号链接和目录替换、分段写入、结果不可覆盖、私有回执与防重放、凭据脱敏、Google RetryInfo／HTTP 冷却及重启后的冷却。桥接测试在 Windows 明确跳过，不把它算作 Windows 桥接通过。使用模拟 HTTP／网关，**不消耗模型额度，不证明 Muse 已端到端接好**。
