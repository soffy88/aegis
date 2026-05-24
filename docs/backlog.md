# Aegis Backlog

## 主库依赖项 (等 Wiki 主库 PATCH)

- [ ] omodul `__version__` 缺失 — 等主库 PATCH bump, 修复后取消 test_omodul_exposes_version 的 xfail
- [ ] omodul 1.10 元数据声明的 oskill>=3.0 是否有上限 — pip check 现在干净, 但主库 MINOR bump 时再确认

## M2 触发后启动

- [ ] ADR-002 升级: event_trail 重试链 (方案 A → C), 触发条件见 ADR
- [ ] aegis_agent 采集 binary 建设
- [ ] aegis_plugins plugin host (BATCH 18 已计划)
