# Aegis 自身接入（C3-7 试点）

Aegis 作为 C3 试点，自己接入 sentry-python 验证全链路。

## 启用条件

仅在 dev/test env 启用，防止 Aegis 监控自身时 platform-postgres 宕机导致递归死循环。

| 环境变量 | 值 | 效果 |
|---|---|---|
| `AEGIS_SENTRY_ENABLED` | `true` | 启用 sentry init |
| `AEGIS_SENTRY_DSN` | `https://<key>@.../envelope/` | DSN |
| `ENV` | `prod` | 即使 ENABLED=true 也不初始化（保险） |

## DSN 获取

```sql
-- Aegis 自身 project（slug = 'aegis' 或 'default'）
SELECT id, sentry_public_key FROM projects WHERE slug = 'default';
```

```
AEGIS_SENTRY_DSN=https://<sentry_public_key>@aegis.kanpan.co/api/<project_id>/envelope/
```

## 接入代码（已在 app.py 实现）

```python
# aegis/server/app.py
def init_sentry_if_enabled():
    if os.environ.get('AEGIS_SENTRY_ENABLED') != 'true':
        return
    if os.environ.get('ENV') == 'prod':
        return
    dsn = os.environ.get('AEGIS_SENTRY_DSN')
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get('ENV', 'dev'),
        release=f"aegis@{os.environ.get('AEGIS_VERSION', 'dev')}",
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        send_default_pii=False,
        integrations=[FastApiIntegration(), StarletteIntegration()],
    )
```

## e2e 验证

```bash
# 1. 设 DSN
export AEGIS_SENTRY_DSN="https://<key>@aegis.kanpan.co/api/<project_id>/envelope/"

# 2. 启 Aegis local server
AEGIS_SENTRY_ENABLED=true uvicorn aegis.server.app:app --port 8000

# 3. 跑 e2e 脚本
make test-c3-e2e

# 4. 查 DB
psql $DATABASE_URL -c "
  SELECT count(*) FROM error_events
  WHERE ts > NOW() - INTERVAL '2 minutes';
"
```

## 测试 endpoint（仅 dev/test）

`POST /api/test/error/?error_type=ValueError`

注册条件：`ENV != 'prod'`（prod 不注册，防误用）。

## 注意事项

- Aegis 监控自己会在 error_events 表写入 Aegis 本身的错误
- 若 DB 宕，Aegis 错误无法写入——但这是可接受的（单人单机 M1 阶段）
- M3+：独立 DB 副本解决此问题（AEGIS-BACKLOG-024）
