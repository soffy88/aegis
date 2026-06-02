# Helios 接入

Helios 是决策辅助 dashboard（Tesla model），含 Python FastAPI backend + Next.js frontend。

## DSN 获取

```sql
SELECT id, sentry_public_key FROM projects WHERE slug = 'helios';
```

## Python Backend 接入

```python
# helios/backend/main.py 或 app factory 入口
import os
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

sentry_sdk.init(
    dsn=os.environ.get('HELIOS_SENTRY_DSN'),
    environment=os.environ.get('ENV', 'dev'),
    release=f"helios@{os.environ.get('HELIOS_VERSION', 'dev')}",
    traces_sample_rate=0.0,
    profiles_sample_rate=0.0,
    send_default_pii=False,
    integrations=[FastApiIntegration()],
)
```

环境变量（docker-compose.yml 加）：

```yaml
HELIOS_SENTRY_DSN: ${HELIOS_SENTRY_DSN:-}
```

## Next.js Frontend 接入

`helios/frontend/sentry.client.config.ts`：

```typescript
import * as Sentry from '@sentry/nextjs';

Sentry.init({
    dsn: process.env.NEXT_PUBLIC_HELIOS_SENTRY_DSN,
    environment: process.env.NEXT_PUBLIC_ENV || 'dev',
    release: `helios@${process.env.NEXT_PUBLIC_HELIOS_VERSION}`,
    tracesSampleRate: 0.0,
    sendDefaultPii: false,
    beforeSend(event) {
        // 过滤 cookie（防 session token 泄漏）
        if (event.request?.cookies) delete event.request.cookies;
        return event;
    },
});
```

## 关键监控点

| 监控点 | 严重度 |
|---|---|
| Fusion decision-assist 计算错 | P0 |
| Layer 0 veto 异常 | P0 |
| WebSocket 连接断（frontend） | P1 |
| Markets > [symbol] 数据加载失败 | P1 |
| Fusion Score 结果 NaN/inf | P1 |

## 量级预期

~50-200 events/day（中等流量）。

## 接入坐标（Wiki 确认后填）

- 入口文件: `<待经理人确认>`
- docker-compose service: `helios-api`
