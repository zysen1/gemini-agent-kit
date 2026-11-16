# 第三方组件声明

Gemini Agent Kit 的原创代码使用仓库根目录的 [MIT License](LICENSE)。这不改变、覆盖或重新授权任何第三方组件的许可证。

## Antigravity Tools / Antigravity-Manager

| 项目 | 信息 |
| --- | --- |
| 上游项目 | [lbjlaq/Antigravity-Manager](https://github.com/lbjlaq/Antigravity-Manager) |
| 本项目固定集成版本 | [v5.0.3](https://github.com/lbjlaq/Antigravity-Manager/releases/tag/v5.0.3) |
| 上游许可证 | [CC BY-NC-SA 4.0](https://github.com/lbjlaq/Antigravity-Manager/blob/v5.0.3/LICENSE) |
| 官方许可证说明 | [署名—非商业性使用—相同方式共享 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans) |
| 用途 | 独立进程处理账号连接、模型目录、额度查询与上游 API 协议 |

本仓库提供原创界面、管理接口适配、生命周期管理和客户端配置辅助，不把上游代码或二进制纳入本仓库的 MIT 许可。组件从上游官方 Release 单独下载或复用已安装的匹配版本；本仓库不随源码分发该二进制，也未修改下载的上游程序。

上游采用署名、非商业、相同方式共享的许可。使用、复制、分享或改编该组件时须遵守其适用条款并保留归属、许可证和变更说明；需要超出该许可证范围的商业使用或分发授权时，应向上游权利人取得相应许可。独立进程的技术架构本身不改变这些条款。

Mac 与 Linux 的支持资产和固定 SHA256 记录在 [manager_runtime.py](scripts/manager_runtime.py)。Windows 提供官方安装入口，由用户执行安装；不会静默运行安装器。

## 其他客户端与网关

文档还介绍 Magpie、Codex、WorkBuddy、Muse、Grok Bot 等独立产品。产品名称与商标归各自权利人所有；它们不因被文档提及而成为本仓库的一部分。本仓库不是 Google、OpenAI 或这些产品的官方项目，也不提供它们的账号、订阅、凭据或模型额度。
