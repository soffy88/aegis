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
| 3 | 闭环自愈接线 (AutoHealEngine + aegis_alert_events) | 🟡 partial | 写入器+repo+retry 已实;**自动执行策略→Needs Human** |
| 4 | 备份执行修复 (result key + 上传) | 🟡 done | router 读对 findings 键+honor status;⚠️真 S3 上传仍是 omodul 桩 |
| 5 | 应用升级/回滚真实执行 | 🟡 done | 接真 omodul dispatch(升级)+rollback_app(回滚);⚠️真 Docker 不可验,upgrade 缺 image 追踪(见 #19) |
| 6 | 安装路径修正 (catalog image/target_host/domain) | ✅ done | catalog image 解析+settings docker/caddy host+domain 回写 |

### P1 — 核心能力缺失
| # | 条目 | 状态 | 验证方式 |
|---|------|------|---------|
| 7 | causal-chain 跨租户泄露 (安全) | ✅ done | 查询锚点+递归均按 org_id 过滤;2 单测 |
| 8 | 节点注册修复 + 心跳 + agent 通信 | 🟡 done | 注册改真 SQL upsert+token;migr 033 加 last_seen/agent_token;heartbeat 端点+status 派生;⚠️edge agent 进程本体属独立二进制不在仓 |
| 9 | 多主机容器控制 (透传 docker_host) | ✅ done | 全部容器端点接受 node_id→解析 docker_host_url 透传 oprim;默认用 settings.docker_host |
| 10 | RBAC 撤权即时生效 (回查 DB) | 🚨 human | 需 auth 核心改造(token_epoch 或每请求回查),风险高,见 Needs Human |
| 11 | 镜像管理域 (list/pull/delete/prune) | ⬜ todo | 单测: 新 router;⚠️真 Docker 不可验 |
| 12 | 网络/卷管理补全 (list/delete) | ⬜ todo | 单测: 端点;⚠️真 Docker 不可验 |
| 13 | RAG embedding provider 注册 | ⬜ todo | 单测: 启动注册 provider |
| 14 | LLM 成本闸改按实际花费 + 可配置 fail-open | ✅ done | 改为按 llm_cost_ledger 真实美元;fail-open 可配置(默认 True,见下说明) |
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
- **#14 LLM 成本闸** — `_check_rca_budget` 从"调用次数代理(Redis INCR)"改为按 `llm_cost_ledger` 真实美元 spend(`org_spend` 滚动 1 天)与日预算比较,更准确。**对审计"改 fail-closed"的建议做了有依据的反驳**:对事件响应工具,Redis/DB 抖动时一律拦截 RCA 会在最需要时致盲;故保留默认 fail-open,但新增 `rca_budget_fail_open` 设置(默认 True),成本优先的运营方可设 False 走 fail-closed。移除无用的 aioredis/datetime/计数推导。test_rca.py (3 重写)
- **#9 多主机容器控制** — 所有容器端点(list/inspect/start/stop/restart/logs/stats/exec)接受 `?node_id=`,经 `_resolve_docker_host` 解析该节点 `docker_host_url` 并透传给 oprik;node_id 省略时用 `settings.docker_host`(顺带修了 REST 路径此前忽略 settings.docker_host 直连本机 socket 的问题)。前端早已发 nodeId,此前被丢弃。test_docker_router.py (+2) + 既有测试适配
- **#7 causal-chain 跨租户泄露** — `causal_chain` 锚点+递归步均加 `org_id` 过滤,端点传 org_id;堵住 A 组织读 B 组织事件链。test_event_trail.py (+1)
- **#8 节点注册修复 + 心跳** — 注册端点从坏掉的 dispatcher 调用改为真实 SQL upsert(按 org_id+node_label),首次注册发 `agent_token`(仅返回一次),复注册保留旧 token;migration 033 给 `aegis_nodes` 加 `agent_token`/`last_seen`;新增 agent-token 鉴权的 `POST /nodes/{id}/heartbeat` 刷新 last_seen;`Node.to_dict` 派生 online/stale/offline 状态。⚠️ edge agent 进程本体(回连/poll 的独立二进制)不在本仓范围。test_nodes_register_heartbeat.py (6)
- **#5 应用升级/回滚真实执行** — `_run_app_lifecycle` 从只打日志的桩改为真实执行:升级走 `OmodulDispatcher.invoke("upgrade_self_hosted_app")`(镜像 `_run_install` 模式),回滚直接调 `omodul.rollback_app.rollback_app`(镜像自愈引擎模式);按真实 `status` 标记 active/failed,不再永远报 active。⚠️ 真 Docker 不可在本环境验;upgrade 的 container_id/new_image 尚未在 installed_apps 追踪(见 #19),在补齐前 upgrade 可能如实报 failed(仍比旧桩诚实)。test_app_lifecycle_exec.py (4)
- **#6 安装路径修正** — 安装请求无 image 时从 store catalog 按 slug 解析 `image`(`find_catalog_app`);`docker_host`/`caddy_admin_url` 改用 settings 而非硬编码;成功后把请求的 `domain` 真正写回(旧代码 domain 变量恒 None)。test_app_lifecycle_exec.py (1) + 既有 install 测试不回归
- **#4 备份执行修复** — `_run_backup` 改为读 `result["findings"].storage_url/total_size_bytes`(旧代码读不存在的顶层键→backup_key 恒 NULL),并 honor `result["status"]`(执行器不抛异常也能报 failed);`_run_restore` 在无 backup_key 时快速失败而非让 boto3 报错。⚠️ 真实 S3 上传仍是 omodul `_stage_upload` 桩(外部库,不改),即 backup_key 会落库但未必指向真对象。test_backups.py (3 改/增)
- **#3 自愈写入器 (partial)** — `AutoHealEventRepository` 给孤儿表 `aegis_alert_events` 加真实写入器;告警 fire 时写事件(severity/source/reason/value),autoheal 看板/stats 从此有数据;retry 端点去掉 TODO 桩,改为真实 `mark_handled`。**自动 signal→remediation 执行未做**:需先有 autoheal 策略模型(无表/无 pattern/无已装插件)+ 是否允许无人值守真实动作的安全决策 → 见 Needs Human。test_autoheal_event_repository.py (4)

## 🚨 Needs Human
- **#10 RBAC 撤权即时生效 (auth 核心决策)**:角色取自 JWT claim(dependencies.py:59-69),降权/移除滞后一个 access TTL。两种正解都需谨慎:① 每个受保护请求回查 DB membership(每请求一次查询的成本 + 所有 require_permission 端点行为变更);② 给 users 加 `token_epoch`,签进 JWT、在 get_current_user 比对 DB、角色变更时自增(立即失效全部 token,但需迁移+改 token 铸造+每请求查 users)。两者都触碰认证核心、测试面广,鲁莽落地有安全风险。建议走②,单独排期 + 安全评审。我未在长会话末仓促改认证。
- **#3 自愈自动执行策略 (产品+安全决策)**:闭环自愈的"自动执行"缺一个把告警信号映射到补救动作的策略模型 —— 当前无 `autoheal_policies` 表、无 `diagnose_pattern_match` 用的 pattern 库、无已安装插件(entry_points 为空)。`AutoHealEngine.run()` 真实但需 `action_plan{patterns, plugin_name, rollback...}`。两个决策需人定:① 策略模型形态(每规则/每应用映射哪个插件+pattern);② 是否允许无人值守执行**真实破坏性动作**(重启/回滚容器),还是默认 `autoheal_dry_run=true` 仅建议。我已把可安全交付的部分(事件写入/看板/retry)做完,未擅自实现自动重启生产容器。
