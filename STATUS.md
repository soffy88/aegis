# Aegis 跟进看板 — 审计整改 (AIOps-12)

> 来源: `AEGIS_AUDIT_20260630.md` 第三部分 22 条优先级清单
> 分支: `feat/aiops-12-audit-remediation` · 基线: 658 passed / 157 skipped (DB-free, mocked asyncpg)
> 决策原则: 长期主义 · 质量为王 · 功能至上 · 不清楚先实证再定
> 完成定义 (DoD): 代码改动 + 单测覆盖 + `pytest` 绿 + 看板更新。依赖外部服务(真 Docker/DB/S3)无法在此环境端到端验证的,标注 ⚠️ 并说明验证边界。

---

## 🔒 Never (绝不做)
- 不改 3O 主库 (obase/oprim/oservice/oskill/omodul) 源码 — 只在 aegis 层适配
- 不 push、不动 main — 全部在 feature 分支
- 不为了"标记完成"伪造测试绿;桩/外部依赖如实标注

## 🔄 In Progress
- (空)

## 📋 Backlog (按优先级,逐条推进)

### P0 — 阻塞基本可用
| # | 条目 | 状态 | 验证方式 |
|---|------|------|---------|
| 1 | Webhook 投递循环 (cron 接 deliver_batch) | ✅ done | 3 单测绿;cron 注册 `_delivery_loop` |
| 2 | 告警规则评估 loop | ✅ done | 4 单测绿;`_alert_eval_loop` + `run_alert_evaluation` |
| 3 | 闭环自愈接线 (AutoHealEngine + aegis_alert_events) | ⬜ todo | 单测: correlator 命中→写事件+调引擎;retry 接真引擎 |
| 4 | 备份执行修复 (result key + 上传) | ⬜ todo | 单测: router 读对键;⚠️真 S3 不可验 |
| 5 | 应用升级/回滚真实执行 | ⬜ todo | 单测: 接 AppInstallerEngine;⚠️真 compose 不可验 |
| 6 | 安装路径修正 (catalog image/target_host/domain) | ⬜ todo | 单测: 从目录读 image、回写 domain |

### P1 — 核心能力缺失
| # | 条目 | 状态 | 验证方式 |
|---|------|------|---------|
| 7 | causal-chain 跨租户泄露 (安全) | ⬜ todo | 单测: 加 org_id 过滤,跨租户 404 |
| 8 | 节点注册修复 + 心跳 + agent 通信 | ⬜ todo | 单测: 注册 SQL upsert;last_seen 列+heartbeat 端点 |
| 9 | 多主机容器控制 (透传 docker_host) | ⬜ todo | 单测: 容器操作带 node 目标 |
| 10 | RBAC 撤权即时生效 (回查 DB) | ⬜ todo | 单测: 降权后立即 403 |
| 11 | 镜像管理域 (list/pull/delete/prune) | ⬜ todo | 单测: 新 router;⚠️真 Docker 不可验 |
| 12 | 网络/卷管理补全 (list/delete) | ⬜ todo | 单测: 端点;⚠️真 Docker 不可验 |
| 13 | RAG embedding provider 注册 | ⬜ todo | 单测: 启动注册 provider |
| 14 | LLM 成本闸改按实际花费 + fail-closed | ⬜ todo | 单测: 超预算拒绝;Redis 失败 fail-closed |
| 15 | On-call 真寻呼 | ⬜ todo | 单测: 升级时按 current_oncall 通知 |

### P2 — 体验/完善
| # | 条目 | 状态 | 验证方式 |
|---|------|------|---------|
| 16 | Release Gate 接入执行 | ⬜ todo | 单测: 部署/自愈前查 gate |
| 17 | 审计覆盖补全 | ⬜ todo | 单测: 敏感操作写 audit_log |
| 18 | 域名 DNS/TLS + 收敛双路径 | ⬜ todo | 单测/⚠️ |
| 19 | 应用多级版本溯源 | ⬜ todo | 迁移+单测 |
| 20 | 日志聚合 | ⬜ todo | ⚠️ 设计为主 |
| 21 | 链路追踪 | ⬜ todo | ⚠️ 设计为主 |
| 22 | Secrets KDF 加固 | ⬜ todo | 单测: 慢 KDF / 独立 master key |

## ✅ Done
- **#1 Webhook 投递循环** — `_delivery_loop` (cron.py) 每 5s 调 `deliver_batch` 排干队列,带 per-tick 批次上限;复用既有重试/退避/死信。test_cron_delivery_loop.py (3)
- **#2 告警规则评估 loop** — `_alert_eval_loop` + `orchestration/alert_evaluation.py`,每 30s 对所有 enabled 规则按 metric 取最近各主机值(>/>= 取 max,</<= 取 min)喂 `evaluate_metric`,命中即写 history+enqueue webhook。新增 `list_all_enabled()`。test_alert_evaluation.py (4)

## 🚨 Needs Human
- (空)
