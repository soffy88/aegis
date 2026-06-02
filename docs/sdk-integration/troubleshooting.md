# C3 SDK 接入故障排查

## 1. DSN 错误（401 / 403 / InvalidDsn）

症状：SDK 启动报错或 Aegis 返回 403。

排查：

```bash
# 手动测试 DSN
curl -X POST \
  -H "X-Sentry-Auth: Sentry sentry_version=7, sentry_key=<your_public_key>" \
  -H "Content-Type: application/x-sentry-envelope" \
  --data $'{"sent_at":"2026-01-01T00:00:00Z"}\n{"type":"event"}\n{"exception":{"values":[{"type":"TestError","value":"ping"}]}}' \
  "https://aegis.kanpan.co/api/<project_id>/envelope/"
```

期望：`HTTP 200 {"id":"..."}`

常见原因：
- `sentry_key` 跟 `projects.sentry_public_key` 不一致 → 重查 DB
- `project_id` UUID 拼错 → 核对 `SELECT id FROM projects WHERE slug = '...'`
- X-Sentry-Auth 格式错（missing `Sentry ` prefix）→ 检查 SDK 版本

## 2. Events 不见（HTTP 200 但 DB 无行）

症状：curl 返回 200，但 `error_events` 表没新行。

排查：

```bash
# 查 Aegis 后端日志
docker logs aegis-api 2>&1 | grep -i "envelope\|error\|ingest" | tail -20

# 查 platform-postgres
docker exec platform-postgres psql -U aegis -d aegis -c \
  "SELECT count(*), max(ts) FROM error_events WHERE ts > NOW() - INTERVAL '5 minutes';"
```

常见原因：
- envelope 解析失败（非 event type item）→ 检查 SDK envelope 格式
- `exception.values` 为空且无 `message` 字段 → SDK 未正确捕获异常
- Ingestor 单 event skip（see `aegis/server/engines/error_ingestor.py`）

## 3. 量级超出预期

症状：`error_events` 行数异常激增。

排查：

```sql
SELECT count(*), date_trunc('hour', ts) AS hour
FROM error_events
WHERE project_id = '<project-id>'
GROUP BY 2
ORDER BY 2 DESC
LIMIT 24;
```

短期缓解：
- SDK 端加 `error_sample_rate=0.1`（仅发 10%）
- M2 加 rate limit（AEGIS-BACKLOG-027）

## 4. PII 泄漏检查

症状：担心 stacktrace vars 含敏感数据。

检查：

```sql
SELECT extra, user_context, tags
FROM error_events
WHERE project_id = '<project-id>'
ORDER BY ts DESC
LIMIT 10;
```

人眼审 `extra` 和 `user_context` 字段。

防护：
- `send_default_pii=False`（默认）
- Helixa 等项目用 `before_send` hook 过滤（见 helixa.md）

## 5. TimescaleDB / platform-postgres 错误

症状：Aegis backend 日志含 `timescaledb not found` 或连接失败。

排查：

```bash
# 验证 timescaledb extension
docker exec platform-postgres psql -U aegis -d aegis \
  -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'timescaledb';"

# 验证 error_events 是 hypertable
docker exec platform-postgres psql -U aegis -d aegis \
  -c "SELECT hypertable_name FROM timescaledb_information.hypertables;"
```

如果 platform-postgres 宕机：Aegis 自身监控失效，同故障域。M3+ 解决（AEGIS-BACKLOG-024）。
