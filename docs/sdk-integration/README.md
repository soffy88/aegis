# Aegis SDK 接入文档

Aegis 提供 Sentry-compatible 错误监控平台，8 个 Helios 生态项目通过 sentry-python / @sentry/browser SDK 接入。

## DSN 获取

1. **M3+**: 登录 Aegis console（AEGIS-BACKLOG-028）
2. **M1 直查 DB**:
   ```sql
   SELECT id, sentry_public_key FROM projects WHERE slug = '<your-project>';
   ```
3. **DSN 拼装**:
   ```
   https://<sentry_public_key>@aegis.kanpan.co/api/<project_id>/envelope/
   ```

## 接入顺序

| # | 项目 | SDK | 优先级 |
|---|---|---|---|
| 1 | Aegis 自身 | sentry-python | 试点（已完成 C3-7） |
| 2 | Helios | sentry-python + @sentry/browser | live trading 监控关键 |
| 3 | Helixa | sentry-python | live trading 最高价值 |
| 4 | Tide | sentry-python + @sentry/react | A 股 trader，用户面前端 |
| 5 | Helivex | sentry-python | 不 live |
| 6 | Selene | sentry-python | 不 live |
| 7 | Stratum | sentry-python | 不 live |
| 8 | Hevi | sentry-python | 不 live |

## 各项目接入文档

- `aegis.md` — Aegis 自身（试点）
- `helios.md` — Helios（Python backend + Next.js frontend）
- `helixa.md` — Helixa（live trading 高优先级）
- `tide.md` — Tide（Python + React mobile-first）
- `python-template.md` — Helivex / Selene / Stratum / Hevi（通用模板）
- `troubleshooting.md` — 常见问题排查

## 全局接入原则

```python
sentry_sdk.init(
    dsn=os.environ.get('..._SENTRY_DSN'),
    environment=os.environ.get('ENV', 'dev'),
    release=f"project@version",
    traces_sample_rate=0.0,   # M1 不做 tracing
    profiles_sample_rate=0.0, # M1 不做 profiling
    send_default_pii=False,   # 防 PII 泄漏（默认）
)
```

- ✅ `send_default_pii=False`（默认，防 cookie/IP 进 stacktrace）
- ✅ `traces_sample_rate=0.0`（M1 不开 tracing，避免量级激增）
- ✅ `release=<project>@<version>`（启用 release tracking，M3+ 真用）
- ❌ 不在 stacktrace 暴露密码 / API key（Helixa `before_send` 过滤示例见 helixa.md）

## 测试接入是否成功

接入后手动触发 1 个测试错误：

```python
import sentry_sdk
sentry_sdk.capture_exception(ValueError("test error from <project>"))
sentry_sdk.flush(timeout=5)
```

然后查 Aegis DB：

```sql
SELECT count(*) FROM error_events
WHERE project_id = '<your-project-id>'
  AND ts > NOW() - INTERVAL '5 minutes';
```

期望 ≥ 1。

## 量级评估（RFC v1 §7.3）

| 项目 | 预期量级 |
|---|---|
| Helios / Helixa（live trading） | 峰值 ~500 events/day |
| Tide / Aegis | ~100-300 events/day |
| Helivex / Selene / Stratum / Hevi | ~30-100 events/day 各 |
| 总计 | ~500-2000 events/day |

当前 Aegis backend + TimescaleDB 承载量足够。

## 故障转移（M3+）

当前（M1）：Aegis 与 platform-postgres 同故障域，单人单机可接受。
M3+：AEGIS-BACKLOG-024 Aegis 独立 pg replica + fallback DSN。
