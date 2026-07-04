#!/usr/bin/env bash
# DESIGN.md C-9c — skip 的测试提供虚假信心。skip 数不得增长，且每个 skip 必带 reason。
# 静态基线记于 .skip-baseline。要降基线（删/修 skip）就改小该文件；要加 skip 会红。
set -euo pipefail

BASELINE_FILE=".skip-baseline"
[ -f "$BASELINE_FILE" ] || { echo "ERROR (C-9c): 缺 $BASELINE_FILE"; exit 1; }
BASELINE=$(tr -d '[:space:]' < "$BASELINE_FILE")

# 静态 skip 标记计数（装饰器 + 运行时 skip + skipif）
COUNT=$(grep -rEn "@pytest\.mark\.skip|pytest\.skip\(|skipif" aegis/tests --include="*.py" | wc -l | tr -d '[:space:]')

if [ "$COUNT" -gt "$BASELINE" ]; then
    echo "ERROR (C-9c): skip 数 $COUNT > 基线 $BASELINE。"
    echo "  新增 skip 不被接受 — 要么真跑（testcontainers），要么删。见 DESIGN.md §9。"
    echo "  确需降基线时，把真实执行的 skip 转真跑后调小 $BASELINE_FILE。"
    exit 1
fi

# 每个 @pytest.mark.skip / skipif 必带 reason=
UNREASONED=$(grep -rEn "@pytest\.mark\.skip(if)?\b" aegis/tests --include="*.py" | grep -v "reason=" || true)
if [ -n "$UNREASONED" ]; then
    echo "ERROR (C-9c): 下列 skip 缺 reason=（必须写明为何 skip、何时转真跑）:"
    echo "$UNREASONED"
    exit 1
fi

if [ "$COUNT" -lt "$BASELINE" ]; then
    echo "C-9c OK: skip 数 $COUNT < 基线 $BASELINE — 建议把 $BASELINE_FILE 调到 $COUNT 锁定收益。"
else
    echo "C-9c OK: skip 数 $COUNT == 基线 $BASELINE，全部带 reason。"
fi
