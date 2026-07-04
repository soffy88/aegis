#!/usr/bin/env bash
# DESIGN.md C-I6 — 生产盘无无界写入者：每类遥测信号必须在保留登记表中有条目。
#
# 保留登记表已接线（aegis/server/persistence/retention.py 定义 RETENTION + EXTERNAL_RETENTION，
# cron._retention_loop 按其分批 prune）。本门禁为硬 gate：新增遥测写入者若其信号类未登记则红。
set -euo pipefail

REGISTRY="aegis/server/persistence/retention.py"

# §7 要求覆盖的信号类（表名以实际迁移为准，落地时对齐）
REQUIRED_SIGNALS=(
    "agent_metrics"        # 指标
    "loki"                 # 日志（外部栈，登记保留窗口）
    "traces"               # 链路
    "event_trail"          # 事件
    "audit_log"            # 审计
)

if [ ! -f "$REGISTRY" ]; then
    echo "ERROR (C-I6): 保留登记表 $REGISTRY 缺失 —— 无界写入者禁入生产盘。见 DESIGN.md §7 / I6。"
    exit 1
fi

MISSING=()
for sig in "${REQUIRED_SIGNALS[@]}"; do
    grep -q "\"$sig\"\|'$sig'" "$REGISTRY" || MISSING+=("$sig")
done

if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "ERROR (C-I6): 下列信号类未在 $REGISTRY 声明保留策略: ${MISSING[*]}"
    echo "  无界写入者禁入生产盘。见 DESIGN.md §7 / I6。"
    exit 1
fi

echo "C-I6 OK: 各信号类均有保留策略。"
