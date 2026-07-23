# Storydex Python wheels

`coomi_agent-1.2.1-py3-none-any.whl` 是 Storydex 固定使用的 Coomi 运行时：

- 发布来源：PyPI `coomi-agent==1.2.1`
- 上游仓库：`https://github.com/Septemc/Coomi`
- Python 要求：`>=3.9`
- SHA-256：`c8d1517150ed8150df626547b448e0f768e6fa60bf06a8108a15e880dc68d6b9`

Storydex 在自身适配层维护 usage、trace、事件与兼容性契约，不再依赖旧版专用 fork 的私有导出。
项目 Python、CI 锁文件、vendor wheel、桌面内嵌 Python 与安装包必须使用同一版本和同一 wheel。
