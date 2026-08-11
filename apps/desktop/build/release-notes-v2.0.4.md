# Storydex v2.0.4

本次发行完成 Storydex Android 剧情创作系统的项目级重构，并同步增强桌面端共用的 Coomi Rust 运行时。桌面版版本为 2.0.4，Android 测试版版本为 0.1.2。

## 主要更新

- Android 剧情设置采用七类横向导航：基础设置、风格预设、随机系统、剧本管理、记忆系统、时间系统和主题外观。
- 风格预设、剧本、记忆、时间与自定义随机词库全部随故事项目保存，切换项目互不影响。
- 新增结构化记忆检查点、最长十段推理周期、连续性审校、故事时间推进和 OOC 拒绝保护。
- 上下文圆环可查看当前容量、分类用量、分模式项目累计、当前与平均缓存命中率，并支持新建统计周期。
- 随机事件和随机人物支持分类 JSON 导入导出、男女词库、内置恢复和同轮因果链拟合。
- 文件管理补齐内联条目菜单、长按多选、复制剪切、冲突处理和永久删除确认。
- 拓展管理按“故事创作”和“通用工具”展示，并内置面向长篇创作的检索、连续性与写作能力。

## 发行产物

- `StorydexSetup-x64-2.0.4.exe`
- `StorydexSetup-x64-2.0.4.exe.blockmap`
- `Storydex-win-unpacked.zip`
- `Storydex-Android-arm64-v0.1.2.apk`
- `latest.yml`、`SHA256SUMS.txt`、`BUILD_MANIFEST.json` 和 `DEPENDENCIES.json`
