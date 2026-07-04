#!/usr/bin/env bash
# DESIGN.md C-4.2 — 有副作用的编排循环必须幂等（宕机追赶不双发）。
# escalation 循环是范式：mark_escalated 以 `escalated_at IS NULL` 条件写实现幂等。
# 本门禁守护该锚点不被回归掉；任何去掉条件写的改动都会让升级循环在宕机追赶时双发。
set -euo pipefail

REPO="aegis/server/repositories/alert_fired_repository.py"

if [ ! -f "$REPO" ]; then
    echo "ERROR (C-4.2): $REPO 不存在 — 升级循环幂等锚点丢失"
    exit 1
fi

# 锚点 1: 领取待升级只取未升级行
# 锚点 2: mark_escalated 的 UPDATE 带 escalated_at IS NULL 守卫
if ! grep -q "escalated_at IS NULL" "$REPO"; then
    echo "ERROR (C-4.2): $REPO 缺失 'escalated_at IS NULL' 条件写守卫。"
    echo "  升级循环失去幂等，宕机追赶会重复 fire。见 DESIGN.md §4.2。"
    exit 1
fi

echo "C-4.2 OK: 升级循环幂等锚点在位。"
