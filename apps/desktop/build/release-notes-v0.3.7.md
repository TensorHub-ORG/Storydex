# Storydex v0.3.7

本版本同时包含原计划 v0.3.6 的稳定性改进，并修复 Windows 环境下的 Agent HTTPS 连接问题。

## AI 稳定性与诊断

- 提升 AI 对话与任务执行在不同安装环境下的稳定性和一致性。
- 加强启动时的运行环境检查，依赖异常时提供更明确的诊断信息。
- 完善安装包与便携包的兼容性验证，降低环境差异导致的启动和运行异常。

## Windows HTTPS 连接修复

- 修复部分 Windows 环境调用 LLM Provider 时持续出现 `APIConnectionError` 或 `SSLEOFError` 的问题。
- 优化 Python 运行时选择，避免迁移环境中不匹配的 OpenSSL 组件影响 HTTPS 连接。
- 内嵌运行环境保持统一并经过发布前验证。

## 安装方式

- 已安装 v0.3.5 及更早版本的安装版用户，可在 Storydex 内检查更新并升级到 v0.3.7，也可下载完整安装包覆盖安装。
- 便携版用户请下载 v0.3.7 便携包并重新解压。
- 安装包与便携包均内置 Python 3.9、后端依赖和 MinGit，无需另行安装 Python 或 Git。
