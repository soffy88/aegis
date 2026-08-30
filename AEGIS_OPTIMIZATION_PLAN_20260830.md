# Aegis 优化计划 — 2026-08-30

> 基于 `AEGIS_AUDIT_20260830.md` 审计结果 + `STATUS.md` 整改看板 + `DESIGN.md` 设计文档
> 决策原则: 长期主义 · 质量为王 · 功能至上 · 不清楚先实证再定
> DoD: 代码改动 + 单测覆盖 + `pytest` 绿 + 看板更新

---

## 一、当前阶段定位

**阶段**: "闭环已接线、待真机验证"（v0.2.5+）

2026-06-30 审计的 6 条 P0 阻塞项已全部闭合。剩余工作的核心矛盾是：

> **Execute 环已接线但未在真实基础设施上验证（L1），而设计文档要求 L2 以上才能默认启用。**

因此下一阶段的本质目标：**把 Execute 环从 L1 推到 L2**。

---

## 二、优化优先级矩阵

### 🔴 P0 — 必须先解决（阻塞演进）

| 优先级 | 条目 | 负责人 | 验收标准 | 工期 |
|---|---|---|---|---|
| P0-1 | **M2 命运解耦核实** | 部署 | 确认被管交易负载与 `platform-postgres` 非同实例；同实例则迁离或物理隔离 | 1d |
| P0-2 | **`aegis-autoheal-sdk` 生产镜像** | 镜像 | `Dockerfile.prod` 包含 `aegis-autoheal-sdk`；autoheal 测试跳过数归零 | 0.5d |
| P0-3 | **RAG embedding provider 注册** | 后端 | `app.py register_providers()` 注册 `fastembed` 或配置 `ollama_base_url`；`test_embedding_provider_warning.py` 绿 | 0.5d |
| P0-4 | **I7 暴露姿态确认** | 部署 | 确认 tunnel 前强制 Cloudflare Access 策略；无策略则在部署文档中标记"禁止裸公网运行" | 0.5d |

**P0-1 理由**: M2 铁律已破（共享 `platform-postgres`），但 Aegis 仍在生产运行。需先明确是被管负载迁移还是 Aegis 迁离，否则所有"运维平台"声明为空话。

**P0-2 理由**: `aegis-autoheal-sdk` 未装时 autoheal 测试 skip，这不是"桩"，这是"import 失败"。先解决依赖可见性问题，再谈真机验证。

### 🟡 P1 — 核心能力升级（L1→L2）

| 优先级 | 条目 | 负责人 | 验收标准 | 工期 |
|---|---|---|---|---|
| P1-1 | **S1 演练场景落地** | 后端+测试 | 杀 canary 容器 → ≤60s 告警 → 自愈重启（仅 `aegis-canary` 标签目标）→ 通知送达 → `event_trail` 完整 | 5d |
| P1-2 | **真 Docker 端到端验证** | 测试 | `testcontainers[postgres]` + `docker` 真依赖跑通容器全生命周期（安装→启停→升级→回滚→卸载） | 5d |
| P1-3 | **S3 死人开关验证** | 测试 | 心跳静默 → L1 外部 watchdog 触发；注入投递管道故障 → L2 旁路触发 | 3d |
| P1-4 | **Webhook 真投递验证** | 测试 | `aegis_w_webhook_secret` 配置 → `enqueue_event` → `deliver_batch` → 外部 HTTP 端点收到 | 2d |
| P1-5 | **备份真 S3 端到端** | 测试 | 真实 S3 bucket → `backup_app_data` 上传 → `restore_from_backup` 下载 → 内容校验 | 3d |
| P1-6 | **应用升级真实 Docker** | 测试 | 部署 2 容器 → 升级 → 验证新镜像运行 → 回滚 → 验证旧镜像恢复 | 3d |

**P1-1 理由**: DESIGN §9 S1 是 Execute 环 L2 的入场券。必须在真实容器上验证"杀容器 → 告警 → 自愈 → 通知"全链路。

**P1-2 理由**: 当前测试大量 skip 是因为缺少真 Docker/DB。`testcontainers` + `docker` 是 L2 的基础设施。

### 🟡 P2 — 设计合规补齐

| 优先级 | 条目 | 对应条款 | 验收标准 | 工期 |
|---|---|---|---|---|
| P2-1 | **降级模式 (degraded mode)** | DESIGN §11.1 + C-11 | 启动断言：前置条件不满足时 auto 自愈禁用、R1+ 人工门、Brain 只读；`/health` 返回 `degraded` | 3d |
| P2-2 | **成熟度降级规则** | DESIGN §2.1 + C-2.1 | 演练结果写入后断言：连续 N 次失败自动置 L1 + 禁 auto | 2d |
| P2-3 | **全局急停开关** | DESIGN §5.3 + C-5.3 | `POST /system/emergency-stop` 一键关闭全部 auto 自愈；S3 验证 | 2d |
| P2-4 | **L2 前仅 canary 标签** | DESIGN §5.4 + C-5.4 | engine 断言：非 `aegis-canary` 目标且未过 L2 → 拒绝执行 | 1d |
| P2-5 | **canary 标签围栏 API 层** | DESIGN §9 + C-9a | 服务端拒绝 harness 对非 `aegis-canary` 资源的动作 | 1d |
| P2-6 | **S2 越界安全断言** | DESIGN §9 + C-9b | cleanup 只触碰 allowlist 路径；越界即演练失败 | 2d |
| P2-7 | **skip 测试治理** | DESIGN §9 + C-9c | `scripts/check_skip_baseline.sh`：静态 skip 数 ≤ `.skip-baseline` 且每个 skip 带 `reason=` | 0.5d |
| P2-8 | **loop-runner advisory lock** | DESIGN §4.1 + C-4.1 | `_acquire_loop_runner_role` 已实现，需确认 prod 部署多实例时锁生效 | 1d |

**P2-1 理由**: DESIGN §11.1 明确要求前置条件未满足时以 degraded mode 运行。当前无此机制——前置条件第一天就被违反但系统无降级运行条款。

### 🟢 P3 — 体验/完善

| 优先级 | 条目 | 负责人 | 验收标准 | 工期 |
|---|---|---|---|---|
| P3-1 | **循环拆进程** | 后端 | 仅当 self-metrics 实测到某循环饿死其它循环时执行；拆进程解锁 API 多 worker | 5d |
| P3-2 | **链路追踪控制台** | 前端 | OTLP ingest 已实现，控制台 trace 瀑布/依赖图页面 | 3d |
| P3-3 | **日志聚合控制台** | 前端 | Loki + promtail 已实现，控制台日志页可视化 | 2d |
| P3-4 | **成熟度仪表盘** | 前端 | 每项能力展示 L0-L4 徽章 + 演练历史 + 降级状态 | 3d |
| P3-5 | **多主机 edge agent** | 独立二进制 | `aegis-agent` poll/WS/enroll 通信；节点↔server 真实双向通信 | 10d |
| P3-6 | **CI/CD 集成** | 部署 | Git webhook → auto-deploy；入站 webhook 触发部署 | 5d |

---

## 三、执行环 L1→L2 验证路线图

```
Week 1:  P0-1 M2 核实 + P0-2 SDK 镜像 + P0-3 embedding 注册 + P0-4 暴露确认
Week 2:  P1-1 S1 演练场景（杀 canary → 告警 → 自愈 → 通知）
Week 3:  P1-2 真 Docker 端到端 + P1-4 Webhook 真投递
Week 4:  P1-3 S3 死人开关 + P1-5 备份真 S3 + P1-6 应用升级真 Docker
Week 5:  P2-1 降级模式 + P2-2 成熟度降级 + P2-3 急停开关
Week 6:  P2-4 canary 围栏 + P2-5/S2 越界 + P2-7 skip 治理
Week 7:  P3-1 循环拆进程 + P3-4 成熟度仪表盘
Week 8:  全量 S1-S4 演练通过 → Execute 环升至 L2
```

---

## 四、关键依赖与风险

### 外部依赖
- **真实 Docker 宿主机**: P1-2/P1-5/P1-6 必须可访问 Docker daemon。建议部署一个专用 canary 节点。
- **真实 S3 bucket**: P1-5 需要一个可写的 S3 目标。建议用 MinIO 或 AWS S3。
- **`aegis-autoheal-sdk`**: 需确认 `helios-plat/aegis-autoheal-sdk@v0.1.0` 已发布到可安装位置。
- **Ollama / fastembed**: P0-3 需要一个 embedding provider 可用。

### 风险
1. **M2 铁律已破**: 如果被管负载确实与 `platform-postgres` 同实例，这不是"优化"而是"架构修正"，需要决策迁离方案。
2. **S1 演练可能失败**: 自愈引擎从未在真实容器上跑过。需在 canary 标签围栏内先行，避免影响生产。
3. **`aegis-autoheal-sdk` 兼容**: SDK 版本 `v0.1.0` 可能与 `oskill`/`omodul` 版本不兼容。
4. **测试 skip 膨胀**: 当前 166 skipped 中部分是真依赖缺失，需区分"桩"和"不可验"。

---

## 五、度量指标

| 指标 | 当前 | 目标 (8 周后) |
|---|---|---|
| Execute 环成熟度 | L1 | L2 |
| 测试通过率 | 1009 passed / 166 skipped | ≥1200 passed / ≤50 skipping |
| Skip 测试中"真依赖缺失"占比 | ~60% | ≤10% |
| Design 符合性矩阵覆盖 | 7/21 | ≥15/21 |
| 演练场景 S1-S4 | 0/4 | 4/4 |
| 降级模式 | 未实现 | 已实现 |
| 全局急停开关 | 未实现 | 已实现 |

---

## 六、决策记录

### 已决策
- **LLM 成本闸 fail-open 保留**: 对事件响应工具，Redis/DB 抖动时一律拦截 RCA 会在最需要时致盲。保留默认 fail-open，但新增 `rca_budget_fail_open` 设置（默认 True）。
- **installer.py 死代码删除**: `AppInstallerEngine` 无任何调用者且依赖从未配置的远程 catalog URL，真安装走 apps.py+omodul+内置 catalog。一并移除 3 个 appstore_* settings 与其测试。
- **RAG 走 pg_trgm 保底**: 未注册 embedding provider 时大声告警，注册真实模型属 deploy 配置。
- **多主机 edge agent 进程本体不在仓**: 独立二进制，本仓只管理 server 侧。

### 待决策
- **M2 命运解耦方案**: 被管负载迁离 vs Aegis 迁离 → 需部署团队确认
- **canary 标签策略**: 全局默认关闭，仅 `aegis-canary` 标签目标可 auto → 需产品确认
- **降级模式触发条件**: 哪些前置条件未满足时触发 degraded mode → 需按 DESIGN §11 逐条映射
