# Aegis 智能运维平台 — 全功能审计 / 标杆对标 / 差距清单

> 审计日期: 2026-06-30 · 方法: 真实代码扫描 (router/service/engine/migration/前端 page 逐项读码确认)
> 范围: 后端 `~/projects/aegis` (FastAPI + 3O 主库 obase/oprim/oservice/oskill/omodul) · 前端 `~/projects/aegis/aegis-console` (Next.js)
> 规模: **133** 个 API endpoint (33 router) · **30** 张表 · **35** 个前端页面 · **5** 个后台 cron 循环
>
> 完成度判定: ✅ 完整可用 (前后端贯通 + 真实数据流) · 🟡 部分 (单端缺失/数据空/依赖外部库/有桩) · 🔴 骨架 (有路由无实现/TODO/无生产调用方/已知坏)
> 每条结论标注代码出处。**有路由 ≠ 可用**;凡判断均以函数体实际行为为准,不以命名/注释为证。
> 关键争议项(自愈接线、webhook 投递、节点心跳、密码哈希、备份执行、租户隔离)均由主审**二次复核源码**后锁定,见文末"判定可信度"。

---

## 三个核心结论 (先看这里)

**🔴 最大短板 — "执行/Act 与投递/Deliver" 层在边缘大面积空心化(且会给出"成功"假象)。**
平台能"看"(采集/异常检测)和"想"(RCA/计划生成),但真正"动手"和"发出去"的闭环大量是桩、坏或未接线:
- **闭环自愈不运行**:`AutoHealEngine` (autoheal/engine.py:163) 是真实四阶段状态机,但**全仓无任何生产调用方**(仅测试);retry 端点调用的 `autoheal_cycle` (autoheal.py:21-31) 是只打日志的 TODO;`aegis_alert_events` 表**从无 INSERT**(migrations.py:598 建表)。
- **告警规则永不自动触发**:cron 无规则评估 loop(cron.py:138-146);`evaluate_metric` 仅被 error-spike 手动路径调用,docstring 自承"M2 才有 scheduler"(error_alerter.py:71)。
- **Webhook 永不投递**:订阅/签名/重试/死信全实现且有单测,但**实际投递 `deliver_batch` (webhook_dispatcher.py:99) 无任何生产调用方**——cron 只 enqueue (alert_escalation.py:64) 不 deliver;事件在 `webhook_delivery_queue` 里堆积但永远不发出。**这等于告警/事故的对外通知通道整体失效。**
- **备份/恢复不可用**:上传是桩(`s3_upload_file` 导入但从不调用;size/checksum 硬编码),且 router 读了执行器根本不返回的结果键 `storage_url`/`total_size_bytes`(backups.py:163-164)→ `backup_key=NULL, size_bytes=0`,导致即便恢复代码是真的也无 key 可下载。
- **应用升级/回滚是纯 DB 记账**:`_run_app_lifecycle` (apps.py:272-296) 只打日志翻状态,不换容器/镜像;真实 compose 引擎 (installer.py) 是死代码无 router 调用。
- **无多主机控制面**:跨主机容器**只读**可用(nodes.py:99 真调 `docker_ps(docker_host=...)`);但 start/stop/restart 忽略节点、只打本机 socket(docker.py:100-108),前端 `?nodeId=` 被静默丢弃。节点注册端点签名错配 + 依赖缺失 omodul,**几乎必坏**。

**🟢 最大优势 — AIOps Brain 是"真智能",且认证/多租户是生产级地基。**
- Brain 不是套壳模板:RCA 是基于 Claude Sonnet 的 agentic ReAct loop,注入 **14 个真实诊断工具**(pg/rabbitmq/docker/网络/系统/磁盘,rca.py:53-69)+ RAG (rca.py:157-189);triage 用 Haiku 真打分;action planner 产出绑定真实插件注册表的 JSON 步骤;LLM 走官方 `anthropic` SDK、模型 `claude-sonnet-4-6`/`claude-haiku-4-5`(app.py:105-134, config.py:174-182)。这是 Portainer/Coolify/Dokploy **完全没有**的能力域。
- 认证地基扎实:Argon2id(OWASP 默认 t=3/m=64MB/p=4)+ JWT HS256 access/refresh + jti 撤销 + 中心化 5 角色 Permission 矩阵逐端点 `require_permission`(auth/rbac.py)。核心流程对标 Portainer Teams / Coolify RBAC 无显著差距。

**🚨 P0 必修(否则"运维平台"名不副实):**
1. **接上 webhook 投递循环** — 否则所有对外通知(告警/事故/escalation)永远发不出去,是当前最隐蔽的整域失效。
2. **接上告警规则评估 loop** — 否则用户配置的告警永远不会响。
3. **接上闭环自愈** — 给 `AutoHealEngine` 触发器 + 写 `aegis_alert_events`,否则头牌"自愈"在生产完全不执行。
4. **修复备份执行 + 应用升级/回滚执行** — 现状改 DB 状态会给"成功了"的假象(实际什么都没动)。

> 安全提示(非功能但需注意):`GET /events/{id}/causal-chain` 的底层查询 `WHERE id=$1` **未按 org_id 过滤**(event_trail.py:196,而同文件 list/get 都过滤了 line 166)→ A 组织成员可读取 B 组织的事件因果链(跨租户读泄露)。

---

# 第一部分 · 现状盘点 (功能 × 完成度)

## 1.1 后端能力清单 (133 endpoints / 33 router,完成度汇总)

| 域 | router | 端点数 | 完成度 | 一句话现状 (出处) |
|----|--------|-------|--------|------------------|
| 容器生命周期 | docker.py | 11 + 1 WS | 🟡 | 启停/重启/日志/exec/WS终端全真(docker.py:100-382);无删容器/镜像;REST 单机(忽略 docker_host) |
| 镜像管理 | — | 0 | 🔴 | oprim 有 pull/list/del/prune,**零 REST 端点零前端**;build 全栈缺失 |
| 应用部署/Store | apps.py, store.py | 8 | 🟡/🔴 | 目录可浏览(仅元数据);安装委托缺失 omodul 且硬编码 localhost;升级/回滚是桩(apps.py:272-296) |
| 多主机/节点 | nodes.py, edge.py | 8 | 🔴 | 跨主机容器**只读**可用;注册端点签名错配几乎必坏;无心跳字段;control 不分主机 |
| 监控/指标 | metrics.py, scrape_targets.py | 10 | 🟡 | scrape 真(15s loop)、存 Postgres(非TSDB);**规则评估loop缺失** |
| 告警 | alerts.py, alert_rules.py, alert_fired.py | 9 | 🟡 | 规则CRUD真、push-ingest真;**规则不会自动触发,告警 webhook 也不投递** |
| 自愈 | autoheal.py | 4 | 🔴 | 引擎真但无调用方;retry是TODO(autoheal.py:21-31);事件表无写入 |
| Runbook | runbooks.py | 5 | ✅ | 真执行 docker+shell 步骤、审批门、落盘(runbook.py:172-206) |
| 事件关联/事件流 | events.py, incidents.py | 11 | ✅ | 实时聚类+因果cron;incident全生命周期+LLM postmortem(⚠causal-chain 跨租户泄露) |
| On-call | oncall.py | 4 | 🟡 | 排班CRUD+轮值计算真;但无人被实际寻呼 |
| 修复学习 | remediation.py | 1 | 🟡 | 真记账+回喂planner;非ML |
| Brain (AIOps) | brain.py | 5 | ✅ | RCA/triage/plan 真LLM;向量库真但 embedding provider 未注册 |
| 备份 | backups.py | 5 | 🔴 | 记录+UI真;**上传是桩 + router 读错结果键→backup_key=NULL**,端到端不可用 |
| Secrets金库 | secrets.py | 4 | ✅ | AES-256-GCM 真加密+轮转(token_encryptor.py:29-32, secrets_vault.py:63-71) |
| 域名 | domains.py | 3 | 🟡 | domains.py 仅存串且 UI 不用它;真反代在 edge.py/CaddyEdge;无 DNS/TLS 自动化 |
| Webhook(出站) | webhook_subscriptions.py | 8 | 🔴 | CRUD/签名/退避重试全实现且有单测,但**deliver_batch 无生产调用方→永不投递** |
| Release Gate | release_gates.py | 4 | 🟡 | 审批状态机真;但 `get_active_gate_by_event` 零调用方→不挡任何部署/自愈 |
| 状态页 | status_page.py | 2 | ✅ | MTTA/MTTR聚合+公开状态页(status_page.py:23,62) |
| 认证 | auth.py | 5 | ✅ | Argon2id+JWT HS256+刷新轮换+jti撤销(auth.py:80-260) |
| 用户/组织/项目/邀请 | users/orgs/projects/invite.py | 25 | ✅/🟡 | 多租户+5角色RBAC+成员管理+邀请;RBAC 角色取自JWT非DB(撤权滞后一个TTL) |
| 审计 | audit.py | 1 | 🟡 | 治理类操作写audit_log,覆盖不全(建org/转移所有权/项目CRUD/邀请/容器均不写) |
| 健康/信封/测试 | health/envelope/test_error.py | 4 | ✅ | 探活/Sentry信封ingest |

## 1.2 前端页面清单 (35 page.tsx,全部真实 fetch,无 mock)

| 路由 | 用途 | 数据流 |
|------|------|-------|
| `(auth)/login`, `register` | 登录/注册 | ✅ 真(open-redirect 防护 login:42-57) |
| `invites/[token]` | 邀请预览/接受 | ✅ 真(public, zod 校验) |
| `(dashboard)/`, `orgs/[slug]/` | 组织列表/仪表盘 | ✅ 真 |
| `…/containers` `/[name]` | 容器列表/详情(stats 3s/日志/exec/xterm WS终端) | ✅ 真 |
| `…/apps` `/[id]` `/install` | 应用列表/详情/安装表单 | ✅ UI真(安装后端委托缺失 omodul,升级/回滚是桩) |
| `…/store` `/[app_slug]` | App Store 浏览/详情 | ✅ 真(仅元数据目录,无 compose) |
| `…/nodes` `/[node_id]` | 节点列表/详情 | 🟡 列表真;`containers_running` 列恒"—"(后端不返回);control 按钮 nodeId 被丢弃 |
| `…/metrics` | 指标图表(自绘SVG折线) | ✅ 真 |
| `…/alerts/ingest` | 手动测试触发告警 | ✅ 真(测试工具性质) |
| `…/projects/[p]/alert-rules` `/[rule_id]` | 告警规则CRUD/详情 | ✅ 真;"Recent fires"实际常空(评估loop缺失) |
| `…/projects/[p]/release-gates` `/[gate_id]` | 发布门审批 | ✅ 真(但审批结果不被任何流程消费) |
| `…/runbooks` `/executions/[id]` | Runbook 列表/执行时间线/审批 | ✅ 真 |
| `…/autoheal` | 自愈统计/事件/retry | 🟡 真UI;后端表无数据→恒为空,retry 打到桩 |
| `…/incidents` `/[id]` | 事件列表/详情/ack/resolve/postmortem | ✅ 真 |
| `…/backups` | 备份列表/创建/恢复 | 🟡 UI真;size 列恒"—"(后端存0);执行不可用 |
| `…/webhooks` `/[sub_id]` | Webhook 订阅/测试/投递历史 | 🟡 UI真;"enqueued ✓"措辞诚实(只入队不投递) |
| `…/brain` | Brain 调试台(triage/investigate/plan/status) | ✅ 真(诚实空态) |
| `…/domains` | 域名管理 | 🟡 真,但实际打 `edge/routes`(Caddy)而非 domains.py |
| `…/events` `/[event_id]` | 事件流/因果链 | ✅ 真(⚠causal-chain 后端跨租户泄露) |
| `…/projects` `/[p]` | 项目CRUD/健康(真HTTP探活+SSRF防护) | ✅ 真 |
| `…/settings/members` | 成员/角色/邀请 | ✅ 真(owner 行不可改/删) |

**前端整体**: 35 页**全部**走真实 `aegisFetch` + react-query,**无 mock 数据**;`aegisFetch` 自动注入 Bearer + 静默刷新一次。前端成熟度显著高于后端执行层——多处是"真UI打在空/桩/坏后端上"(nodes 容器列、autoheal 事件、alert-rules recent fires、backups size、webhooks 投递)。

## 1.3 数据模型 (30 张表 @ migrations.py)

| 分组 | 表 |
|------|-----|
| 租户/身份 | `orgs`, `projects`, `users`, `org_memberships`, `org_invites`, `revoked_tokens` |
| 审计/事件 | `event_trail`, `audit_log`, `incident_events` |
| 应用/域名 | `installed_apps`(含 `previous_version` 单级溯源 migr.032:788), `domains` |
| 告警/规则 | `alert_rules`, `alert_fired_history`, `aegis_alert_events`(**孤儿表,无写入**) |
| 指标/异常 | `agent_metrics`, `metric_anomalies`, `scrape_targets` |
| 事故 | `incidents`, `remediation_outcomes` |
| 节点 | `aegis_nodes`(**仅 registered_at,无 last_seen/status/heartbeat 列**), `aegis_backups` |
| Webhook | `webhook_subscriptions`, `webhook_delivery_queue`(**只进不出**) |
| 发布/排班 | `release_gates`, `oncall_schedules` |
| 错误追踪 | `error_events`, `error_issues` |
| 金库/成本 | `org_secrets`(GCM 密文+key_version), `llm_cost_ledger` |
| 系统 | `aegis_migrations` |

> 实体 `models/`: org/project/user/membership/node 轻量 dataclass + repository 模式(repositories/ 下 13 个 repo)。
> **数据模型设计普遍超前于执行层实现** — 多张表/字段(`aegis_alert_events`、`webhook_delivery_queue`、node 字段)已就位但无生产写入/消费方。

---

# 第二部分 · 标杆对标矩阵 (能力域 × 差距)

> 标杆: Portainer (容器/集群/Teams) · Coolify/Dokploy (应用部署/auto-deploy/app store) · Grafana 栈 (可观测) · 各平台均**无** Aegis 级别的 AIOps 智能层。

| 能力域 | 标杆基线 | Aegis 现状 (代码出处) | 具体差距 |
|--------|---------|----------------------|---------|
| **容器生命周期** | Portainer: 启停/重启/删/暂停/日志/stats/exec/attach/重命名全覆盖 | 🟡 启停/重启 (docker.py:100-139)、日志 (142)、stats (161)、一次性 exec (237)、**WS 交互式终端** (260-382) 全真,docker-py SDK | **缺**:删除/暂停/重命名/创建容器;REST 路径忽略 `docker_host` 只连本机 socket(仅 WS 终端 docker.py:311 honor) |
| **镜像管理** | Portainer: 拉取/构建/列表/删除/prune 全覆盖 | 🔴 oprim 有 pull/list/del/prune 但**零 REST 端点零前端**;build 全栈缺失 | **缺整个域** |
| **多主机/集群** | Portainer **Edge Agent**(反向隧道/poll)、跨环境;Rancher 多集群 | 🔴 跨主机容器**只读**真(nodes.py:99 调 `docker_ps(docker_host=url)`,需 docker_host_url+oprim);注册端点 `OmodulDispatcher` 构造/invoke 签名错配 + omodul 缺失→几乎必坏(nodes.py:43-66);`aegis_nodes` 无任何心跳/状态列(migr.578-592);control(start/stop)忽略节点打本机(docker.py:100-108),前端 nodeId 被丢弃 | **缺**:edge agent 进程、节点↔server 通信(无 poll/WS/enroll)、心跳、跨主机**控制**与调度;"edge"实为 Caddy 反代≠计算节点 |
| **应用部署** | Coolify/Dokploy: Git 推送→构建→部署、compose、一键 app store(含 compose 模板) | 🟡 目录 14 应用(builtin.json,**仅元数据无 compose**);安装委托 `omodul.install_self_hosted_app`、`target_host` 硬编码 localhost、**忽略目录 image**(apps.py:89-106);真 compose 引擎 (installer.py:144-189) **从无 router 调用** | **缺**:升级/回滚真执行(现为桩 apps.py:272-296)、Git/源码部署、compose 模板;`domain` 回写恒 None(apps.py:77,132 bug) |
| **网络/Volume** | Portainer: 网络/卷 列表/创建/删除/inspect 全覆盖 | 🟡 网络 create+del (docker.py:183-214)、卷 create (217) 真;**无 list、卷无 delete 端点、无任何前端** | **缺**:网络/卷列表与管理 UI;卷删除端点(oprim 有未接) |
| **监控可观测** | Grafana+Prometheus+Loki+Tempo: 指标/日志/告警/链路全栈 | 🟡 scrape 真(15s loop→httpx→prom 解析,cron.py:106 + metrics_scraper + prometheus_parse);EWMA 异常检测真 (anomaly.py:25-58) | **缺**:真 TSDB(现 Postgres `agent_metrics`+SQL 分桶)、**告警规则评估 loop(关键缺失)**、日志聚合(无 Loki/ES)、链路追踪(无 OTel/Jaeger;trace_id 仅关联串) |
| **自动化** | Coolify: Git webhook→auto-deploy、定时、健康自愈重启 | 🟡 **5 个真 cron loop**(cron.py)、Runbook 真执行(docker+shell+审批门,runbook.py:172-206) | **缺**:CI/CD、入站 webhook 触发部署;**闭环自愈不运行**(引擎无调用方);**出站 webhook 也不投递**(deliver_batch 无调用方) |
| **备份恢复** | Portainer: 配置导出;Coolify: DB/卷定时备份到 S3 | 🔴 记录+UI 真,但上传是桩(`s3_upload_file` 从不调用,size/checksum 硬编码)+router 读错结果键(backups.py:163-164)→backup_key=NULL;restore 代码真但无 key 可下载 | **缺**:可用的备份执行、S3 目标接线、定时备份调度;**当前端到端不可用** |
| **权限/多租户** | Portainer Teams: 用户/团队/RBAC/资源授权 | ✅/🟡 Argon2id+JWT HS256(access/refresh+jti 撤销)、5 角色(owner/admin/operator/member/viewer)Permission 矩阵逐端点强制(rbac.py:85-153)、org 路径作用域、成员/角色/转移所有权、邀请(token+TTL) | **细项差距**:RBAC 角色取自 JWT claim 非 DB→撤权/降权滞后一个 access TTL(dependencies.py:59-69);`causal-chain` 跨租户读泄露(event_trail.py:196);审计覆盖不全;无 SSO/OIDC;无资源级细粒度授权 |
| **智能能力 (AIOps)** | **标杆全无** — 三家均无 RCA/异常检测/智能建议 | ✅ **差异化优势**:agentic RCA(Claude Sonnet+14 诊断工具+RAG)、EWMA 异常检测、真事件关联 cron、LLM action planner、LLM postmortem、成本账本 | **本域 Aegis 领先**;待补:embedding provider 未在 app.py 注册→RAG 实际未必可用(vector_store.py 真但未配)、成本闸是调用次数非美元且 fail-open(rca.py:290-335)、**Brain 只产计划不自动执行**(brain.py:94 止于 action_plan_ready) |

---

# 第三部分 · 差距 + 优化清单 (可执行,按优先级)

> P0 阻塞"作为运维平台"基本可用 · P1 核心能力缺失 · P2 体验/完善

| 优先级 | 条目 | 现状 (出处) | 目标 | 涉及模块 | 工作量估 |
|--------|------|------------|------|---------|---------|
| **P0** | Webhook 投递循环 | `deliver_batch` (webhook_dispatcher.py:99) 无生产调用方;cron 只 enqueue(alert_escalation.py:64)不 deliver | 新增 `_delivery_loop` 周期调 `deliver_batch`(已含重试/退避/死信),接入 cron | orchestration/cron.py, engines/webhook_dispatcher.py | S (0.5-1d) |
| **P0** | 告警规则评估 loop | cron 无规则评估(cron.py:138-146);`evaluate_metric` 无周期调用方(error_alerter.py:71) | 新增 `_rule_eval_loop` 周期查 `alert_rules` 对 `agent_metrics` 求值→写 `alert_fired_history`+enqueue webhook | orchestration/cron.py, engines/alert_engine.py | M (1-2d) |
| **P0** | 闭环自愈接线 | `AutoHealEngine`(engine.py:163)无调用方;`autoheal_cycle`(autoheal.py:21-31)是 TODO;`aegis_alert_events` 无写入 | correlator/规则评估命中策略时写 `aegis_alert_events` 并调 `AutoHealEngine.handle`;retry 端点接真引擎 | orchestration/event_correlator.py, autoheal/engine.py, routers/autoheal.py | L (3-5d) |
| **P0** | 备份执行修复 | 上传桩(s3_upload_file 不调用,size/checksum 硬编码)+router 读错键 storage_url/total_size_bytes(backups.py:163-164) | 真正上传到 S3 并回写正确 key;router 读执行器真实返回键;校验 restore 端到端 | routers/backups.py, omodul/backup 模块 | M (2-3d) |
| **P0** | 应用升级/回滚真实执行 | `_run_app_lifecycle` 只打日志翻状态(apps.py:272-296);真 compose 引擎是死代码(installer.py) | upgrade/rollback 背景任务接 `AppInstallerEngine.install_app` 或 omodul,真正换容器;回滚用 `previous_version` | routers/apps.py, appstore/installer.py | M (2-3d) |
| **P0** | 安装路径修正 | 硬编码 `target_host=localhost`、忽略目录 image、`domain` 回写恒 None(apps.py:89-132) | 从 store 目录读 image/ports/mounts;按 project/node 传 target_host;回写真实 domain | routers/apps.py, store.py | S (1d) |
| **P1** | causal-chain 跨租户泄露(安全) | `causal_chain` 查询 `WHERE id=$1` 无 org_id 过滤(event_trail.py:196) | 查询加 `AND org_id=$2`,与 list/get 一致 | persistence/event_trail.py | XS (0.5d) |
| **P1** | 节点注册修复 + 心跳 + agent 通信 | 注册端点 dispatcher 签名错配 + omodul 缺失(nodes.py:43-66);无 last_seen 列;control 不分主机 | 修正注册(或改原生 SQL upsert);加 `last_seen` 列 + `POST /nodes/{id}/heartbeat`(校验 token);start/stop 传目标 docker_host | routers/nodes.py, migrations.py, models/node.py | L (5d+) |
| **P1** | 多主机容器控制 | 跨主机**读**已通(nodes.py:99);start/stop/restart 忽略 node 打本机(docker.py:100-108),前端 nodeId 被丢 | 容器操作接受并透传 docker_host(沿用 WS 终端 docker.py:311 做法) | routers/docker.py | M (2-3d) |
| **P1** | RBAC 撤权即时生效 | 角色取自 JWT claim,不回查 DB→撤权滞后一个 access TTL(dependencies.py:59-69) | 敏感操作回查 membership,或缩短 access TTL + 主动失效 | auth/dependencies.py, auth/rbac.py | S-M |
| **P1** | 镜像管理域 | 无任何 `/images` 端点与页面(oprim 已有 pull/list/del/prune) | 新增镜像 list/pull/delete/prune 端点 + 前端页面 | (新)routers/images.py, console | M (2-3d) |
| **P1** | 网络/卷管理补全 | 仅 create(+网络 del)有端点,无 list、卷无 del,无前端(docker.py:183-234) | 补 list/delete 端点 + 管理页面 | routers/docker.py, console | M (2d) |
| **P1** | RAG embedding provider | `embedding_provider=default` 但 app.py 只注册 LLM provider(vector_store.py:24, app.py register_providers) | 启动注册真实 embedding provider,否则 RCA 知识检索静默失效(rca.py:184-186) | server/app.py, services/vector_store.py | S (0.5-1d) |
| **P1** | LLM 成本闸改按实际花费 | 闸是调用次数(rca.py:290-335)且 Redis 出错 fail-open | 改按 `llm_cost_ledger` 美元;Redis 失败 fail-closed | services/llm_cost.py, brain/rca.py | M |
| **P1** | On-call 真寻呼 | 排班+轮值真,但无人据此被通知(oncall.py:88 仅被读) | 事件/告警触发时按 `current_oncall` 经 webhook/邮件/IM 寻呼 | engines/alert_escalation.py, services/oncall.py | M |
| **P2** | Release Gate 接入执行 | 状态机真但 `get_active_gate_by_event` 零调用方(release_gate_service.py:108);router 未注入 webhook_dispatcher | 部署/升级/自愈前检查对应 gate 状态;注入 dispatcher 发 approved/rejected 事件 | engines/release_gate_service.py, routers/apps.py & autoheal | S-M |
| **P2** | 审计覆盖补全 | 建org/转移所有权/项目CRUD/邀请/容器操作均不写 audit_log(audit.py;persistence/audit.py) | 上述敏感操作补 `record_audit` | 各 router, persistence/audit.py | S |
| **P2** | 域名 DNS/TLS + 收敛双路径 | domains.py 仅存串且 UI 不用;真反代在 edge.py/CaddyEdge;无 DNS 记录管理 | 统一到 CaddyEdge 路径 + 加 DNS provider + 证书状态回显 | edge/caddy.py, routers/domains.py | M |
| **P2** | 应用多级版本溯源 | 仅 `previous_version` 单级(migr.032:788) | 加 `app_version_history` 表支持多级回滚 | migrations.py, routers/apps.py | S |
| **P2** | 日志聚合 | 无(仅 Sentry 式错误 ingest error_ingestor.py) | 接 Loki/容器日志流式聚合 + 查询 UI | (新)服务, console | L |
| **P2** | 链路追踪 | 无 OTel/Jaeger;trace_id 仅关联串(alerts.py:43) | 接 OpenTelemetry collector + trace 视图 | (新), console | L |
| **P2** | Secrets KDF 加固 | 主密钥 = `secrets_master_key` 或 `sha256(jwt_secret)`,后者非慢 KDF(secrets_vault.py:21-25) | 强制使用独立高熵 master key 或慢 KDF;支持外部 KMS | services/secrets_vault.py | S |
| **P2** | Brain 计划→执行闭环 | brain pipeline 止于 `action_plan_ready`,不执行(brain.py:94) | 经审批门后将 action plan 交 plugin 执行(与自愈共用执行层) | orchestration/brain.py, autoheal/engine.py | M |

---

## 附:判定可信度说明

- 8 个能力域由独立子代理并行读码;其中 **nodes/auth/backups/automation 域出现首轮与复跑结论不一致**,主审**逐一回源码裁决**,以下为锁定事实(均经 grep/读码二次确认):
  - **自愈未接线**:`maybe_autoheal` 全仓不存在(grep 空);`autoheal_cycle` 确为 TODO(autoheal.py:21-31);`AutoHealEngine.handle` 生产调用方为 0(仅 tests);`aegis_alert_events` 仅建表无 INSERT。→ 🔴
  - **Webhook 不投递**:`enqueue_event` 被 escalation loop 调用(alert_escalation.py:64),但 `deliver_batch`(webhook_dispatcher.py:99)在 `aegis/server` 内**无任何调用方**,cron 不驱动投递。→ 🔴
  - **密码哈希 = Argon2id**(obase `argon2_hash`/`argon2_verify`,auth.py:10/94/135),非 bcrypt;token = JWT **HS256**。→ 认证 ✅
  - **节点无心跳列**:`aegis_nodes` 仅 `registered_at`(migr.578-592),无 last_seen/status;首轮"按 last_heartbeat 衰减状态"为误判。注册端点 dispatcher 签名错配 + omodul 缺失,几乎必坏;但 `/{id}/containers` 确做真实远程 `docker_ps(docker_host=url)`(nodes.py:99,需 url+oprim)。
  - **备份不可用**:`s3_upload_file` 导入但 0 次调用、size/checksum 硬编码(backup_app_data.py),且 router 读执行器不返回的键 `storage_url`/`total_size_bytes`(backups.py:163-164)→ backup_key=NULL。→ 🔴
  - **跨租户泄露**:`causal_chain` 查询 `WHERE id=$1` 无 org_id 过滤(event_trail.py:196)。
- 3O/omodul 等外部库内部实现不在本仓;凡"委托外部库"项标 🟡/🔴 并注明依赖(本 checkout 中 omodul/oprim 未安装,相关执行路径会落 failed/报错)。
- 前端 35 页全程真实 fetch,无 mock;多数 🟡/🔴 源于"前端真实但后端空/桩/坏"。
