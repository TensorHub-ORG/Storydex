# Storydex v0.3.7

## Agent HTTPS 连接修复

- 修复部分 Windows 环境调用 LLM Provider 时持续出现 `APIConnectionError: Connection error` 的问题。
- 根因是迁移任意 Conda Python 环境后可能保留不匹配的 OpenSSL DLL，并在 HTTPS 握手时触发 `SSLEOFError`。
- 项目运行时现在优先使用明确配置的 Python、官方 `py -3.9` 或系统 Python 3.9，仅在没有标准运行时时回退到 Conda。
- 内嵌运行环境继续严格固定并验证 `coomi-agent==1.1.2`。

## 安装方式

已安装 v0.3.5 及更早版本的用户可通过应用内更新升级到 v0.3.7。安装包与便携包继续内置 Python 3.9、后端依赖和 MinGit，无需另行安装 Python 或 Git。
