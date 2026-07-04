# Aegis 设计文档

> 本文件是 Aegis 运行与演进的唯一设计依据。规范性语气：文中 **MUST / MUST NOT / SHALL** 为强制约束，违反即缺陷；**SHOULD** 为默认取向，偏离需在设计中显式论证。任何新增能力先对照本文的不变量（§1）与资产判定（§2）自证，不满足者不予接线。每条 MUST 的执法方式见**附录 B 符合性矩阵**（CI / 运行时断言 / 演练 / 人工评审四类之一）——未进矩阵的 MUST 视为未执法。不变量 I1–I9 与该矩阵的修改 MUST 走 ADR（`docs/adr/`）。运行时的实时状态、待办与验证边界记录在 `STATUS.md`（运行看板），本文只描述系统**应当如何被设计与运行**。

---

## 0. 目的与不变量

### 0.1 定位
Aegis 是**单机、单团队**的自托管运维控制面。它在一台宿主机上闭合一条 MAPE-K 控制环：

```
Monitor → Analyze → Plan → Execute → Knowledge → (回灌 Monitor)
监控       分析       规划    执行       知识
```

其差异化不来自"能看"（观测栈可替代）或"能动"（容器管理可替代），而来自**在同一台机器上把观测与动作闭合成一个可审计、有护栏的环**。因此：

> **闭环是价值单元。任何不加强某一环、或加宽却不闭合的功能，是 scope creep，不予接线。**

### 0.2 铁律
> **运维平台必须比它所管理的对象更可靠。** Aegis 管理实盘交易系统等生产负载，故其自身的可靠性、可观测性与失败模式，优先级高于其任何管理能力。凡与此铁律冲突的设计，铁律胜。

铁律 MUST 以可证伪形式落地，否则是宣言：
- **M1 — 可用性偏序**：Aegis 告警管道的可用性目标 MUST ≥ 其所管理负载中最高的 SLO。管道弱于被管对象时，铁律已破。
- **M2 — 命运解耦**：Aegis 自身的 Postgres 实例 MUST 与被管负载的数据库隔离——不共享实例、不共享数据盘。当前部署 Aegis 挂在共享 `platform-postgres`（Helios 基座）上，此 MUST 的符合性 MUST 对照"被管交易负载是否同实例"核实（附录 B / §11）；同实例即命运耦合，铁律从第一天为空话。

### 0.3 边界（SHALL NOT）
Aegis 的设计边界是**一台机器、一个小团队**。以下明确不做，属于工具选型错配，不在本系统职责内：
- Kubernetes 风格的集群编排、跨主机调度、GPU 跨项目调度；
- 多云 / 跨机中心化面板；
- 独立消息总线（Kafka 等）——投递语义由 Postgres outbox 承载（§4.3）。

K8s 只读接入以**外部只读视图**存在，不承担编排职责；在存在真实 K8s 集群作为管理对象之前，该域冻结（§2.3）。

---

## 1. 架构不变量

以下不变量是全系统的承重结构，凌驾于任何单域设计之上。

**I1 — 闭环治理。** 系统能力按 MAPE-K 环归属（§3）。闭环的整体强度由**最弱一环**决定。投资 MUST 优先流向最弱环；持续加宽已最强的环（如 Monitor）而不闭合执行链，视为倒挂，不予接线。

**I2 — 成熟度定资产。** 每一项能力 MUST 标注成熟度（§2，L0–L4）。**Execute 环中成熟度 < L2 的能力计为负债，MUST NOT 计为资产，MUST NOT 默认启用。**

**I3 — fail-closed 钉在执行环。** fail-closed 的适用范围是**改变被管系统状态的动作**（自愈、破坏性操作、凭据变更）：依赖不可达时 MUST fail-closed（停手）。守护**建议**的门（Brain/LLM 成本闸）MAY fail-open——其最坏后果是成本，而非错误动作。**通知路径既不 fail-closed 也不 fail-open，而是 fail-loud**：本管道投递失败 MUST 触发 §6 L1/L2 旁路，MUST NOT 静默停手（否则违反 I4）。三态边界——fail-closed（状态变更动作）/ fail-open（建议）/ fail-loud（通知）——MUST NOT 混淆。

**I4 — 不静默失败。** 告警送达路径的每一环 MUST 有一条**不依赖 Aegis 自身**的旁路（§6）。Aegis 与被监控对象同机部署，故"Aegis 挂了谁告诉你"MUST 有确定性答案。

**I5 — 相关非因果。** 关联分析（correlation / Brain RCA）MUST 在证据只支持相关时停在相关，MUST NOT 将时间邻近呈现为因果结论。因果依据只来自显式声明的拓扑（§10.3）与变更事件（§10.1）。

**I6 — 有界写入者。** 生产盘上 MUST NOT 存在无界增长的状态。每一类遥测信号 MUST 有保留期、降采样与预算（§7）。平台自身 MUST 守护其存储占用。

**I7 — 暴露层级。** Aegis 属**自用组**控制面，MUST NOT 直接暴露于开放公网。对外可达 MUST 经外置强认证层（Cloudflare Access 策略或 tailnet），应用自身鉴权 MUST NOT 作为唯一门。

**I8 — Postgres 即总线。** 事件投递以 Postgres outbox + 轮询排干实现（§4.3）。其语义 MUST 显式声明为**至少一次、无序**，消费者 MUST 幂等。

**I9 — 自举分离。** 部署者不自管。Aegis 本体由宿主机 systemd/compose 管理，MUST NOT 纳入自身 AppStore 自管——避免"谁部署部署者"的循环依赖。

---

## 2. 成熟度阶梯与资产判定

### 2.1 阶梯
每一项能力按其**已验证的最高等级**分类：

| 级 | 名称 | 判定标准 |
|----|------|---------|
| L0 | 设计 | 仅有设计/接口，无实现 |
| L1 | 代码+单测 | 有实现与单元测试，依赖以桩/mock 替代 |
| L2 | 真实环境验证 | 在真实基础设施（真 Docker/DB/S3/网络）上端到端跑通目标场景 |
| L3 | 生产稳定 | 在生产负载下持续正确运行 |
| L4 | 事故淬炼 | 经历过至少一次真实故障并完成复盘，护栏据此加固 |

**降级规则（成熟度可降）。** 成熟度不是一次性认证。晋升靠 §9 演练转绿，**降级 MUST 自动触发**：任一 L2 及以上能力连续 N 次（默认 2）对应演练失败，MUST 自动降回 L1——即重新计为负债，其 auto 模式自动禁用（I2），直到重新转绿。无降级规则则"验证即监控"名不副实：绿灯必须能变红。

### 2.2 资产规则
- **Execute 环**（自愈插件、破坏性动作、发布/凭据操作）：< L2 计为**负债**，禁止默认启用（I2）。负债的危害是"会咬人"，其消除是**紧急项**。
- **非 Execute 环**：< L2 的低价值能力（如单用户价值≈0 的观测页）计为**零资产**——冗余而非危险，随时可删，清理非紧急。
- 负资产与零资产 MUST NOT 争抢同一优先级：前者动手前不得计为能力，后者是有空再清的杂物。

### 2.3 冻结
一项能力当其**管理对象在本部署中数量为零**，或**单机单用户场景下价值≈0**时，SHALL 冻结：保留已有代码与数据模型，停止功能投入。冻结不是删除，是停止倒挂投资。

---

## 3. 域架构（按 MAPE-K 环）

每环列出其域、职责与设计约束。域名对应 `aegis/server/api/routers/` 与前端页。

### 3.1 Monitor — 监控（采集事实）
| 域 | 职责 | 约束 |
|----|------|------|
| metrics / scrape-targets | Prometheus 抓取、派生速率 gauge | 抓取器与规则解耦；每目标自带 interval 门 |
| uptime-targets | HTTP 拨测 | SHOULD 顺带做 TLS 证书到期检查（握手即得） |
| telemetry (ingest) / APM / RUM / 链路 | OTel 遥测接入 | **无埋点即空壳**：APM/链路页 MUST 先能回答"哪个 project 真的发了 trace"，否则冻结 |
| loki | 日志聚合 | 受 §7 保留策略约束 |
| profiling | 性能剖析（Pyroscope） | 按需启动，非常驻 |
| slo / status-page / status-components | SLO 与对外状态 | 单机场景冻结 status-page 增量 |
| cron dead-man | 定时任务（hevi 流水线、helivex 采集器类）存活监控 | cron 型负载挂掉比容器隐蔽（容器活着、活儿没干）。MUST 以预期心跳建模：到期未心跳即告警，机制与 §6 L1 同构 |

设计取向：Monitor 是当前最强环，**MUST NOT 继续加宽**，除非新信号直接喂入更弱的 Analyze/Plan 环（I1）。RUM 单用户价值≈0，冻结。

### 3.2 Analyze — 分析（事实变判断）
| 域 | 职责 | 约束 |
|----|------|------|
| alert-rules / alert-fired | 阈值规则评估 | 规则 MUST 支持 `for` 持续时间语义（连续满足才 fire）；评估幂等 |
| anomaly | EWMA 异常检测 | MUST 先收敛到人工圈选的指标集，MUST NOT 全指标铺开 |
| correlation / event-correlator | 事件关联、因果链（`event_trail`） | 关联 MUST 跨租户按 `org_id` 隔离；对变更邻近度加权（§10.2） |
| incidents | 事件聚类成事故 | — |
| 维护窗口 / 静默 / 抑制 | 抑制已知无信息量的告警 | 宿主机 down MUST 抑制其上全部容器告警（否则每次 sf1 重启即告警风暴，长期训练人忽略告警——比无告警更危险）。静默窗口内 R1 自动动作 MUST 同步暂停（与 §5 联动） |
| 变更冻结窗口 | 高风险时段禁改 | 冻结窗口内 MUST 禁止部署与自动自愈；§9 演练调度 MUST 尊重冻结窗口（S2 磁盘压测撞行情剧烈时段不可接受） |

缺口（设计要求）：Analyze MUST 补齐**拓扑维度**（§10.3）与**变更维度**（§10.1）——二者是 RCA 的因果前提。告警 SHOULD 具备质量指标（被处理率），低于阈值即判定为噪音源并回收规则。

### 3.3 Plan — 规划（判断变提案）
| 域 | 职责 | 约束 |
|----|------|------|
| brain（rca / triage / action-planner） | LLM 辅助根因、分诊、动作规划 | **建议式，人决策**（§5.1）。受 I5 约束：无拓扑/变更事件时 MUST 停在相关 |
| autoheal-policies | 策略驱动的自愈提案 | dry-run + 冷却门（§5） |

Brain 的 RCA 上限由数据决定：其质量取决于 Analyze 是否提供拓扑与变更事件，而非模型能力。故 Plan 环的投资 MUST 优先补 Analyze 的数据，而非扩 LLM 提示。

### 3.4 Execute — 执行（提案变动作）★ 断链处
| 域 | 职责 | 约束 |
|----|------|------|
| autoheal（engine + plugins） | 执行自愈动作 | 全部动作 MUST 经风险分级（§5.2）与全局熔断（§5.3）；破坏性动作 MUST 人工门 + 强制 dry-run |
| release-gates | 发布门禁 | approve/reject 发一等事件 |
| remediation | 修复统计与执行记录 | — |
| docker / apps / images / networks / volumes | 容器与镜像生命周期 | 多主机经 `docker_host` 透传（§3.7） |

> Execute 是**价值最高、风险最高、且当前唯一从未在真实基础设施上验证**的环（L1）。按 I2，其现状计为负债。消除路径是 §9 的验证模型，**不是重写**——执行邻近链路已具并发安全意识（如 escalation 的幂等条件 UPDATE），病根是"从未在真机上跑过"。

### 3.5 Knowledge — 知识（动作回灌经验）
| 域 | 职责 | 约束 |
|----|------|------|
| runbooks（indexer + vector-store） | Runbook RAG（LanceDB，1024 维） | **RAG 无语料即空转**：MUST 以真实故障模式的 runbook 为语料，否则该能力计为 L0 |
| remediation-learning | 从修复结果学习 | 依赖 Execute 环产生真实修复记录，故其成熟度受 Execute 上限约束 |
| postmortem | 事故复盘 | 与 §10 变更事件联动 |

### 3.6 横切：平台治理（Platform）
租户模型 `org → project`。域：auth / orgs / projects / users / invite / audit / secrets / firewall / security / domains。设计约束见 §8。除多租户数据模型保留外，功能投入冻结至出现第二个真实租户（§2.3）。

### 3.7 横切：基座（Substrate）
- **PaaS**：store / apps / docker / git-deploy / files / compose / host-terminal。核心缺口：**config-as-code 对账**——git 声明态 vs 实际运行态的漂移检测。验收单是"干净机器 30 分钟重建"。
- **多主机 / 边缘**：nodes（注册 + 心跳 + `agent_token`）、edge routes、`aegis-agent`（collector/reporter/loop，独立二进制）。方向正确，保留。
- **K8s 只读**：冻结（§0.3）。

---

## 4. 运行时架构

### 4.1 进程模型
后台编排循环（§4.2）当前与 API **同进程**。若循环随进程实例数复制，则每个循环各跑一份，导致告警评估、投递等双重执行。**用机制取缔纪律，MUST NOT 依赖"单 worker"约定：**

> 循环 supervisor 启动时 MUST 获取一把 Postgres advisory lock 作为 **loop-runner 角色锁**；拿不到锁的实例只跑 API、不启循环。

三个结构性收益：多 worker 立即安全（第二实例自动退化为纯 API）、双重执行在结构上不可能（非约定）、评估器拆进程时迁移路径已铺好（锁持有者从 API 进程换成独立进程即可）。此机制替换任何"MUST 单 worker"表述。

在此之上，循环生命周期与 API 的解耦分两步，MUST 按序：
1. **循环监督**（先做，成本低）：每个循环为独立受监督 task，supervisor 崩溃即重启并计数；每循环打 `last_run / duration / errors` 时间戳（同时作为 §6 L3 self-metrics 的数据源）。
2. **评估器拆进程**（后做，需证据）：仅当 self-metrics **实测到**某循环饿死其它循环时执行。拆进程真正解锁的是 API 恢复多 worker 能力。MUST NOT 在测得饿死之前预付分布式协调成本。

### 4.2 编排循环
| 循环 | 周期 | 幂等/语义要求 |
|------|------|--------------|
| 事件关联 | 5 min | 按 `org_id` 隔离 |
| 容量检查 | 60 min | — |
| 告警升级 | 2 min | MUST 幂等：`mark_escalated` 为 `WHERE escalated_at IS NULL` 条件 UPDATE，宕机追赶不双发 |
| 指标抓取 | 15 s（每目标 interval 再门控） | — |
| 异常扫描 | 60 s（EWMA） | — |
| Webhook 投递 | 5 s（排干队列） | 至少一次；重试/退避/死信（§4.3） |
| 派生记录 | 30 s | — |
| 拨测 | 20 s（每目标 interval 再门控） | — |
| 自愈策略 | 30 s | 冷却 + dry-run 门（§5） |
| 告警评估 | 30 s | 阈值规则对最新指标，命中写 history + 入队 |
| 指标降采样 rollup | 60 min | 落地 §7 的 5min 降采样；MUST 幂等（按时间桶去重），否则保留策略是未接线的空条款 |

**追赶语义**：任何循环 MUST 在宕机恢复后不产生重复副作用。凡有对外副作用（fire、投递、执行）的循环，MUST 通过条件写或去重键保证幂等；升级循环是范式，其它循环对齐。

### 4.3 Postgres 即总线（I8）
事件投递以 outbox 表 + 5s 轮询排干实现，非独立消息中间件。语义**显式**：
- **至少一次**：投递失败重试、退避、死信；消费者 MUST 幂等。
- **无序**：不保证跨事件顺序。
- 延迟敏感路径 MAY 引入 `LISTEN/NOTIFY` 降低轮询延迟，但语义不变。

此设计是对 §0.3"不做消息总线"的正确落地，MUST 在文档与消费者契约中如实标注，MUST NOT 隐含 exactly-once 或有序保证。

---

## 5. 安全与执行模型

### 5.1 决策原则
> **Aegis 建议，人决策。** 系统中 MUST NOT 存在自主执行的 AI agent。Brain 产出提案，动作的批准权在人（或明确分级的自动门，§5.2）。

### 5.2 插件风险分级
自愈插件按**误触发后果**分级，级别决定自动化边界：

| 级 | 类别 | 插件（示例） | 自动化策略 |
|----|------|------------|-----------|
| R0 | 只读/通知 | `notify_oncall` | 可自动 |
| R1 | 可逆动作 | `restart_container`/`restart_service`/`flush_cache`/`clear_queue`/`reconnect_db` | 经真实环境验证（L2）后可自动，受全局限流 |
| R2 | 资源破坏 | `cleanup_disk`/`scale_down`/`drain_node` | **永远人工门 + 强制 dry-run** |
| R3 | 凭据 | `rotate_credentials` | **移出自愈**，仅作手动 runbook 存在——误触发会把运维者锁在所有系统外 |

分级是 §2.2 负资产判定在执行层的落地：级别越高，越需真机验证与人工门。

### 5.3 全局熔断与抖动检测
在既有 `circuit_breaker_check`（`autoheal/engine.py`，来自 oskill）与 per-plugin 冷却之上，MUST 增加全局约束：
- **全局限流**：每小时自动动作总数上限（默认 5），跨插件累计。
- **抖动检测**：同一目标 30 分钟内两次自愈后仍异常 → 判定 flapping，MUST 停止对该目标的自愈并升级人工。
- **强制 dry-run**：R2 动作 MUST 先产 dry-run 提案，经批准方可执行。
- **全局急停开关**：MUST 存在一个开关，一键关闭全部自动自愈。凌晨系统行为诡异时，需要的是一个总闸，而非逐条改 policy。此开关本身 MUST 纳入演练（§9 验证其真能停）。

### 5.4 执行前置与范围化启用（I2 落地）
任一自愈场景在**真实基础设施上跑通**（§9 验证）之前，MUST NOT 计为可用能力（桩/mock 提供的是虚假信心）。但验证本身需要执行——"不跑不能验、不验不能跑"的鸡生蛋 MUST 以**范围化启用**破解：

> L2 之前，自愈仅对带 `aegis-canary` 标签的目标启用，全局仍禁。此约束 MUST 同时写入 §9，否则会有人为跑通演练而打开全局开关。

---

## 6. 可信链：监控监控者（I4）

Aegis 与被监控对象同机，单点断电则监控与告警一同静默。故告警路径 MUST 具备四层旁路，每层不依赖 Aegis 自身：

| 层 | 机制 | 独立性 |
|----|------|--------|
| L1 | **外部死人开关**：一条常燃 watchdog 心跳路由到外部服务（healthchecks.io 类），静默即触发外部短信/IM | 完全外部，Aegis 全挂仍告警 |
| L2 | **跨机互守**：多主机 agent 互探对方，通知走 agent 直连 webhook | 不经 Aegis 自身投递管道（§4.3） |
| L3 | **平台 self-metrics**：每循环 `last_run/duration/errors`、队列深度、DB 体积，进自身告警规则 | 依赖 Aegis 部分存活 |
| L4 | **进程内看门狗**：崩溃的 asyncio 循环自动重启并计数 | 进程内（§4.1 循环监督） |

外层优先：L1/L2 覆盖"Aegis 整体失效"，L3/L4 覆盖"Aegis 部分失效"。设计 MUST 保证不存在"监控与告警同时静默且无人知晓"的窗口。

---

## 7. 数据生命周期（I6）

每类遥测信号 MUST 有保留、降采样与预算。默认策略（可按部署调整，但 MUST 存在）：

| 信号 | 保留 | 降采样 |
|------|------|--------|
| 指标（原始） | 15 d | 之后 5 min 降采样保留 90 d |
| 日志（loki） | 14 d | — |
| 链路（trace） | 7 d | 采样 |
| 事件（event_trail） | 180 d | — |
| 审计（audit） | 1 y | 不降采样 |

**存储守卫**：平台 MUST 监控其自身遥测存储占用，达 70% 触发自告警。最坏情况——可观测栈自己写满生产盘、连同宿主负载一起拖垮——MUST 被此守卫拦截。根分区打满是已发生过的真实故障模式，此约束非可选。

---

## 8. 安全模型

### 8.1 租户
`org → project` 两级。所有 org-scoped 查询 MUST 按 `org_id` 隔离，含关联/因果链的递归锚点（跨租户泄露是安全缺陷）。

### 8.2 认证与授权（诚实态）
当前授权非即时撤权、无账号级审计，属已知未成熟态。设计约束：
- 在满足 I7（外置强认证）之前，MUST NOT 依赖应用自身鉴权作为唯一门。
- 授权撤销的即时生效（token epoch 或每请求回查）是高风险核心改造，MUST 单独设计、独立验证，MUST NOT 顺带改动。
- 多租户数据模型保留，功能投入冻结至第二个真实租户（§3.6）。

### 8.3 Secrets 诚实降级
主密钥与密文同盘存储**是混淆而非加密**。设计 MUST 诚实呈现：
- 未配置独立 master key 时，派生自 `jwt_secret` 无域分离，MUST 启动告警。
- 要么密钥外置（sops/age 类），要么在能力描述中如实标注"进程内密文存储"，MUST NOT 宣称为"加密"。
- 更换派生算法会孤立既有密文，故走告警 + 建议轮转，不静默改算法。

### 8.4 暴露姿态（I7）
链路为 `cloudflared → caddy(:8080) → backend/console`；控制面端口不 publish 到宿主，仅经 tunnel 可达；宿主 80/443 为 `websites` 反代功能开放，属独立攻击面 MUST 计入。对外可达性取决于 **tunnel 前是否强制 Cloudflare Access 策略**——此为运行前置（§11），无策略则等同应用自身鉴权为唯一门，违反 I7。

---

## 9. 验证模型

> **验证即监控。** 成熟度（§2）不靠声明，靠常态化演练证明。`aegis-verify` 是自愈的"回测框架"：把 §3.4 断链处的验证做成每周自动刷新的绿灯，而非一次性人工核对。

多主机各常驻一个 **canary 应用**（无害 echo 容器，经 Aegis 自身 AppStore 部署——顺带 dogfood PaaS 链路）。演练结果作为一等事件落库，失败即告警：

| 场景 | 断言链 |
|------|--------|
| S1 | 杀 canary 容器 → ≤60s 告警触发 → 自愈重启（仅对 `aegis-canary` 标签目标，§5.4）→ 通知送达 → `event_trail` 完整 |
| S2 | 向**专用配额目录**写压力文件 → 容量告警 → `cleanup_disk` dry-run 提案 → drill-approver token 审批 → 执行 → 复原。**MUST 增加安全断言**：cleanup 只触碰 allowlist 路径（该目录），越界即演练失败。验证的不只是"能执行"，更是"不越界"——演练价值一半在功能、一半在护栏 |
| S3 | 心跳静默 → 外部死人开关触发（验证 §6 L1/L2 信任链） |
| S4 | 从 git + 备份在干净环境重建 Aegis，计时（验证 §3.7 重建验收单）；MUST 尊重变更冻结窗口（§3.2） |

S2 不腐蚀 §5.2 的 R2 人工门：执行目标由插件在演练模式下**路径 allowlist 硬约束**到专用配额目录，审批用明确标记的 **drill-approver token**（非生产审批人无脑点确认）。生产路径的 R2 人工门不受演练影响。

**演练框架自身护栏。** `aegis-verify` 是全系统唯一被授权定期搞破坏的组件，它自己出 bug 即事故制造机，故 MUST 满足三条自我约束：
- **标签围栏**：harness 只能对带 `aegis-canary` 标签的资源执行动作，且在 **API 层强制拒绝**越界（服务端拒绝，非 harness 自律）。
- **演练窗口**：可配，且 MUST 尊重变更冻结窗口（§3.2）。
- **一键中止**：MUST 提供，且与 §5.3 全局急停协同。

规则：`STATUS.md` 中标为未验证的执行能力，MUST 通过对应场景转绿后方可升级为 L2；连续 N 次失败按 §2.1 自动降级。skip 的测试提供虚假信心——测试 MUST 真跑（testcontainers 类真依赖）或删除，MUST NOT 长期 skip 充数。

---

## 10. 变更即事件与 RCA 数据模型

### 10.1 变更为一等事件
多数生产事故由变更引发。故 git-deploy、应用升级/回滚、策略修改、密钥变更 MUST 全部写成一等 **change 事件**，与遥测事件同库同模型。

### 10.2 确定性关联优先于 LLM
事故视图顶部 MUST 固定"过去 N 小时的变更"。correlation MUST 对变更邻近度加权。这是确定性 JOIN，不需 LLM，却比 Brain 现有能力更接近根因。RCA 投资 MUST 先做此确定性关联，再谈 LLM 推理（呼应 I5、§3.3）。

### 10.3 拓扑显式声明
依赖拓扑 MUST 来自 app manifest 中**手工声明**的依赖（"本应用依赖哪个 DB / 哪个服务"），MUST NOT 依赖 trace 自动发现作为前提。有了显式拓扑，correlation 才具因果依据；在此之前，关联 MUST 停在相关（I5）。

---

## 11. 运行前置条件

系统在满足以下前置条件之前 **MUST NOT 视为 operational**。这些是不变量在部署上的落地，非计划项：

1. **暴露收口（I7）**：确认 tunnel 前强制外置认证策略；否则控制面等同裸公网，不得运行。
2. **外部死人开关（I4 / §6 L1）**：常燃 watchdog 路由到外部服务并验证静默触发。
3. **数据守卫（I6 / §7）**：各信号保留/降采样已配置，存储守卫（70%）已生效。
4. **自身可恢复**：Aegis 自身 DB（承载全部策略、配置、历史）已备份，且完成**一次真实 restore 演练**——未演练的备份等同没有备份。
5. **命运解耦核实（M2 / §0.2）**：核实被管交易负载与 Aegis 的 `platform-postgres` 非同实例；同实例则铁律已破，MUST 迁离或隔离。

### 11.1 降级运行条款
系统当前即在生产运行、管着真实负载，故前置条件"第一天"未满足。一份从第一天就被违反的规范会教会所有人（含 CC）忽略它，故"未 operational"MUST 有可执行含义，而非自我否定：

> 前置条件未满足期间，系统 MUST 以 **degraded mode** 运行：全部自愈 **auto 模式禁用**、R1 及以上动作**全部人工门**、Brain **仅只读**。逐项满足 §11 后对应解除，全部满足方可退出 degraded mode。

其后方可展开执行环验证（§9 S1–S3）、插件分级与熔断（§5）、循环监督（§4.1）、变更事件与手工拓扑（§10）。

---

## 附录 A：能力资产负债表格式

每一项能力 MUST 以下述三列描述，使能力清单成为诚实的资产负债表（§2）：

```
能力 | 闭环环节(M/A/P/E/K/横切) | 成熟度(L0–L4)
```

判定基线：**Execute 环 L2 以下 = 负债**；非 Execute 环低价值 L1 = 零资产。资产负债表的真实性优先于能力数量。

---

## 附录 B：符合性矩阵（执法层）

> 给人读的规范靠自觉，给自动化执行者（FULL AUTO 下的 CC）读的规范靠**门禁**——不可执法的 MUST 会在数个 sprint 内退化为装饰。故每条 MUST MUST 编号并绑定验证方式，取值只有四种合法类别：**CI**（CI 检查）/ **RT**（运行时断言）/ **DRILL**（演练场景 S1–S4）/ **REVIEW**（人工评审项）。无法归入四类者，MUST 降级为 SHOULD 或承认其为愿望。

**变更治理**：不变量 I1–I9 及本矩阵的修改 MUST 走 ADR（对齐既有 `docs/adr/` 永久锚做法），MUST NOT 直接编辑通过。ADR MUST 记录动机、被推翻的旧约束、迁移影响。

| 编号 | 条款 | 验证类别 | 门禁定义 |
|------|------|---------|---------|
| C-I1 | 投资优先最弱环 | REVIEW | 新能力 PR 评审：自证加强哪一环；加宽最强环而不闭合执行链则驳回 |
| C-I2 | Execute<L2 不默认启用 | RT | engine 启动断言：未过 L2 的插件 auto 模式强制关闭，config 不可覆盖 |
| C-I3a | 状态变更动作依赖不可达 fail-closed | RT + DRILL | engine 层断言 + S1/S2 注入依赖故障验证停手 |
| C-I3b | 通知路径 fail-loud | DRILL | S3：注入投递管道故障，断言 L1/L2 旁路触发 |
| C-I4 | 告警路径每环有独立旁路 | DRILL | S3 覆盖 L1/L2；L3/L4 由 self-metrics 演练 |
| C-I5 | 关联停在相关 | REVIEW | RCA/correlation 输出评审：无拓扑/变更证据不得呈现因果 |
| C-I6 | 无界写入者禁入生产盘 | CI + RT | CI：`scripts/check_telemetry_retention.sh`（登记表 `persistence/retention.py` 落地前为非阻断骨架，落地即转硬 gate）；RT：存储守卫 70% 断言 |
| C-I7 | 不裸公网、外置强认证 | REVIEW | §11 前置核实项：确认 tunnel 前 Cloudflare Access 策略 |
| C-I8 | outbox 消费者幂等 | CI | `scripts/check_outbox_consumer_idempotent.sh`：守护 `FOR UPDATE SKIP LOCKED` 原子领取锚点 |
| C-I9 | 部署者不自管 | REVIEW | AppStore catalog 评审：Aegis 本体不得入自身自管清单 |
| C-M1 | 管道可用性 ≥ 被管最高 SLO | REVIEW | 部署核实项 |
| C-M2 | Aegis PG 与被管 DB 隔离 | REVIEW | §11.5 前置核实项：非同实例、非同数据盘 |
| C-2.1 | 成熟度连续 N 次演练失败自动降级 | RT | 演练结果写入后断言：达阈值自动置 L1 + 禁 auto |
| C-4.1 | loop-runner advisory lock | RT | supervisor 启动获取 PG advisory lock；未获锁不启循环 |
| C-4.2 | 有副作用循环幂等 | CI | `scripts/check_loop_idempotent.sh`：守护 `escalated_at IS NULL` 条件写锚点（escalation 为范式） |
| C-5.2 | R2 永远人工门 | RT | engine 层硬编码检查，policy 配置**不可**覆盖 |
| C-5.3 | 全局急停开关存在且可停 | DRILL | 演练触发急停并断言全部 auto 自愈停止 |
| C-5.4 | L2 前自愈仅对 canary 标签目标 | RT | engine 断言：非 `aegis-canary` 目标且未过 L2 → 拒绝执行 |
| C-7 | 各信号保留/降采样/rollup 已接线 | CI + RT | CI：保留配置存在；RT：rollup 循环幂等按桶去重 |
| C-9a | 演练标签围栏 API 层强制 | RT | 服务端拒绝 harness 对非 `aegis-canary` 资源的动作 |
| C-9b | S2 越界安全断言 | DRILL | S2：断言 cleanup 只触碰 allowlist 路径 |
| C-9c | skip 测试禁止长期充数 | CI | `scripts/check_skip_baseline.sh`：静态 skip 数 ≤ `.skip-baseline` 且每个 skip 带 `reason=` |
| C-11 | degraded mode 默认生效 | RT | 前置条件未满足时启动断言：auto 自愈禁用、R1+ 人工门、Brain 只读 |

矩阵未覆盖的 MUST 视为**未执法**，MUST 在下一次 ADR 补入本表或降级。此矩阵是 CI gate 的需求清单来源。
