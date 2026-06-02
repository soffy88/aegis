# Helixa 接入（Live Trading 高优先级）

Helixa 是 BTC 自动交易系统（live since 2026-05-08），错误监控直接关系资金安全，**接入最高价值**。

## DSN 获取

```sql
SELECT id, sentry_public_key FROM projects WHERE slug = 'helixa';
```

## Python Backend 接入

```python
import os
import sentry_sdk

def filter_sensitive_stacktrace(event, hint):
    """过滤 stacktrace vars 里的 API key / secret。"""
    for exc in (event.get('exception') or {}).get('values', []):
        for frame in (exc.get('stacktrace') or {}).get('frames', []):
            if 'vars' in frame:
                for key in list(frame['vars'].keys()):
                    if any(s in key.lower() for s in ('secret', 'key', 'token', 'password', 'api')):
                        frame['vars'][key] = '[FILTERED]'
    return event

sentry_sdk.init(
    dsn=os.environ.get('HELIXA_SENTRY_DSN'),
    environment=os.environ.get('ENV', 'prod'),  # Helixa 主要 prod
    release=f"helixa@{os.environ.get('HELIXA_VERSION')}",
    traces_sample_rate=0.0,
    profiles_sample_rate=0.0,
    send_default_pii=False,
    before_send=filter_sensitive_stacktrace,
    # 自定义 tag 关联 trade context
    before_send_transaction=None,
)

# 补设 scope tag（strategy / asset）
with sentry_sdk.configure_scope() as scope:
    scope.set_tag('strategy', os.environ.get('HELIXA_STRATEGY', 'unknown'))
    scope.set_tag('asset', 'BTC')
```

## 关键监控点（P0 = 立即触发 alerter）

| 错误类型 | 严重度 | 组件 |
|---|---|---|
| Binance API auth 失败（401/403） | P0 critical | trade engine |
| 仓位 reconciliation 不匹配（DB vs Binance） | P0 critical | reconciliation |
| circuit breaker 触发 | P0 critical | risk control |
| RabbitMQ heartbeat 失败 | P1 warning | message bus |
| Strategy signal 异常（NaN/inf） | P1 warning | scalper |
| 滑点超过阈值 | P2 info | execution |

## Aegis 端 Alert Rule 建议

```sql
-- C3-5 ErrorAlerter + C2-2 AlertEngine 配合
INSERT INTO alert_rules (org_id, project_id, name, metric, condition, threshold, severity)
VALUES (
    '<org-id>', '<helixa-project-id>',
    'helixa_critical_errors',
    'project.error_rate.5min', '>=', 1, 'critical'
);
```

任何 P0 错误 → `error.new_issue` webhook → Telegram 通知。

## 量级预期

~50-500 events/day（取决于市场波动 + 策略活跃度）。Live trading 期间偶尔峰值。

## 安全注意事项

- `before_send` 必须过滤 API key / secret（已提供示例）
- 生产环境 DSN 不进 git（通过 docker-compose env_file / infisical 注入）
- Aegis 端 `error_events.extra` 字段也不会存原始 vars（sentry 在 SDK 端过滤）
