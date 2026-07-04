#!/usr/bin/env bash
# DESIGN.md C-I8 — Postgres outbox 投递语义为"至少一次、无序"，消费者必须幂等。
# 投递领取的原子性锚点是 claim_next_batch 的 `FOR UPDATE SKIP LOCKED` + state 转移
# (pending → in_flight)：并发 worker 不会领到同一行，宕机恢复不会重复处理已 succeeded 行。
# 本门禁守护该原子领取不被回归成非原子 SELECT-then-UPDATE。
set -euo pipefail

REPO="aegis/server/repositories/webhook_delivery_repository.py"

if [ ! -f "$REPO" ]; then
    echo "ERROR (C-I8): $REPO 不存在 — outbox 原子领取锚点丢失"
    exit 1
fi

if ! grep -q "FOR UPDATE SKIP LOCKED" "$REPO"; then
    echo "ERROR (C-I8): $REPO 缺失 'FOR UPDATE SKIP LOCKED' 原子领取。"
    echo "  outbox 消费者失去防重保证，多 worker/追赶会双投。见 DESIGN.md §4.3 / I8。"
    exit 1
fi

echo "C-I8 OK: outbox 原子领取锚点在位。"
