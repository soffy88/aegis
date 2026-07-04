"""遥测保留登记表 (DESIGN §7 / I6 / C-I6).

每类遥测信号的保留期。retention 循环(cron._retention_loop)按此调用 oprim.retention_prune
收口无界写入者(生产盘无无界状态);存储守卫按 STORAGE_GUARD_PERCENT 对生产盘占用大声告警。

新增遥测写入者 MUST 在此登记保留策略,否则 CI 门禁(scripts/check_telemetry_retention.sh)红。
"""

from __future__ import annotations

# (table, ts_column, retain_days) —— 每条信号的 TTL
RETENTION: list[dict[str, object]] = [
    {"table": "agent_metrics", "ts_column": "ts", "retain_days": 15},  # 指标原始点
    {"table": "error_events", "ts_column": "ts", "retain_days": 14},
    {"table": "metric_anomalies", "ts_column": "detected_at", "retain_days": 90},
    {"table": "aegis_alert_events", "ts_column": "created_at", "retain_days": 90},
    {"table": "incident_events", "ts_column": "created_at", "retain_days": 180},
    {"table": "event_trail", "ts_column": "ts", "retain_days": 180},  # 事件因果链
    {"table": "audit_log", "ts_column": "created_at", "retain_days": 365},  # 审计不激进删
]

# 外部观测栈信号 —— 保留由栈内组件执法(非本循环 prune),此处登记窗口备查(§7/C-I6)。
# "loki"/"traces" 的天数须与栈配置(Loki compactor retention / Tempo|Pyroscope block_retention)一致。
EXTERNAL_RETENTION: dict[str, dict[str, object]] = {
    "loki": {"retain_days": 14, "enforced_by": "loki compactor retention_period"},
    "traces": {"retain_days": 7, "enforced_by": "tempo/pyroscope block_retention"},
}

# 平台自身遥测存储占用达此百分比 → 大声自告警(最坏:观测栈写满生产盘拖垮宿主)
STORAGE_GUARD_PERCENT = 70.0
