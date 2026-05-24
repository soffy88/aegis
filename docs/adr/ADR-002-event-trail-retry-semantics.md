# ADR-002: event_trail 重试语义 (M1 A / M2 C 路径)

**日期**: 2026-05-24
**状态**: Accepted (M1)
**决策者**: Wiki (Aegis 项目 owner) + Aegis 经理人

## 背景

C0b 引入 dispatcher 模式后, 同一 omodul fingerprint 可能被多次调用 (e.g. 首次 failed, 重试 completed). event_trail 表如何处理这种重试场景, 有三个方案:

- A. DO NOTHING — 保留首次记录, 重试结果不写入
- B. DO UPDATE SET — completed 覆盖 failed, event_trail 永远是最新状态
- C. 多行全写 — 加 attempt_no 字段, 每次重试一行 (完整历史)

## 决议: D = 短期 A, M2 升 C

### M1 (现在): 方案 A
- event_trail.omodul_fingerprint 加 UNIQUE 约束 (单列)
- `INSERT ... ON CONFLICT (omodul_fingerprint) DO NOTHING`
- attempt_no 列加 DEFAULT 1 (M1 不启用, 占位)
- 含义: 同 fp 只保留首次写入, 重试不污染表

### M2 (商业化后): 方案 C
- 触发条件: 商业化 + 多用户 + 重试场景频繁
- 改动:
  1. 删 omodul_fingerprint 单列 UNIQUE
  2. 加 (omodul_fingerprint, attempt_no) 复合 UNIQUE
  3. dispatcher 写入前查 SELECT MAX(attempt_no) WHERE omodul_fingerprint=$1, attempt_no = max + 1
  4. ON CONFLICT 改为 (omodul_fingerprint, attempt_no) DO NOTHING (理论上不会冲突, 防御性)
- 含义: 同 fp 完整重试链可查, postmortem 拿全链

## 为什么不直接选 C

- M1 用户少, 重试链无大价值, 表行数白增
- attempt_no 算法实现复杂 (要查 MAX + 1), 增加 dispatcher 复杂度
- 一旦升 C, 降级回 A 难 (要 dedup 数据)

## 为什么不选 B

B (UPDATE SET) 丢失中间状态信息, 与 Aegis 作为 SRE 平台 "事件链完整" 核心价值冲突. 一旦失败原因要查时, 已经被覆盖, 没了.

## 升级触发条件 (写进 backlog)

M2 升 C 触发 (满足任一即触发评估):
- 付费用户 ≥ 50 (商业化阈值)
- 单月重试事件 ≥ 100 (重试频率阈值)
- 用户主动请求查重试链历史 (功能驱动)

## 实施 owner

- M1: C0b-fix MF1/MF2/MF3 (本次)
- M2: 待触发后另起 RFC → migration
