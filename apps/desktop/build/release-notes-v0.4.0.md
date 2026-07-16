# Storydex v0.4.0

本次更新完成 Provider usage 来源治理，并将 Storydex 的 Coomi 运行时固定到基于官方 1.1.2 制作的专用适配版。

## 用量统计

- 明确区分上游报告、缺失、历史未知和本地估算 usage。
- 支持 OpenAI-compatible 与 Anthropic 返回的输入、输出、缓存和推理 token。
- 流式响应中的累计 usage 快照只记录一次终态值，避免重复累加。
- Provider 未返回 usage 时保持缺失状态，不再用字符估算冒充真实用量。

## 运行与发布稳定性

- 项目 Python、全哈希依赖锁、桌面内嵌 Python 和安装包统一使用 `coomi-agent==1.1.2+storydex.usage1`。
- 专用 wheel 固定来源和 SHA-256，并纳入桌面同步、内嵌运行时和打包资产校验。
- 长会话真实验证覆盖同一会话连续 3 轮、11 次 Provider 请求，usage 覆盖率 100%，录制与回放结果一致。
- 完整后端、前端、桌面打包和 packaged E2E 发布门禁通过。

已安装用户可在 Storydex 内检查更新；便携版用户请下载新版本并重新解压。
