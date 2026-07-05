# Storydex 文档入口

本文只列当前版本仍可作为事实来源的文档。历史计划、阶段汇报、旧设计和一次性变更总结统一放在 `docs/archive/`。

## 当前事实来源

1. `../README.md`：项目定位、启动方式、常用接口和接手顺序。
2. `../SPEC.md`：Storydex 当前项目级最终目标态规格。
3. `Storydex_Agent基座与编排层架构.md`：当前 Agent 基座与 Storydex 编排层的总体架构。
4. `Storydex_Agent核心逻辑与边界.md`：Coordinator、StreamingToolLoop、IntentFrame、Routes、Controller、Pipeline 的职责边界。
5. `Storydex_UI_E2E实际运行问题报告_2026-06-25.md`：最新一次真实 UI E2E 运行记录和已暴露问题。
6. `superpowers/specs/2026-06-23-memory-management-technique-map.md`：当前记忆管理、事实/关系图谱和上下文压缩决策。
7. `superpowers/plans/2026-06-23-context-compression-memory-recall.md`：最新仍保留的实现计划记录。

## 归档规则

1. `docs/archive/` 只用于追溯历史，不作为当前实现的事实来源。
2. 旧轻量 Spec 已归档到 `docs/archive/2026-06-26_specs/Storydex_轻量Spec_2026-06-23.md`；根目录 `SPEC.md` 是当前项目级规格入口。
3. 日期型执行计划、阶段总结、临时汇报和变更总结在完成或被新文档覆盖后归档。
4. 新增稳定架构结论时，优先更新上方当前事实来源，不再新增并列的日期型根级文档。
5. 新增 E2E 或问题记录时，若它替代了旧记录，应将旧记录移入 `docs/archive/`。
