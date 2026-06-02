# Tide 接入（A 股 trader-assist）

Tide 是 A 股交易辅助系统，含 Python backend + React/Vite frontend（移动 first）。

## DSN 获取

```sql
SELECT id, sentry_public_key FROM projects WHERE slug = 'tide';
```

## Python Backend 接入

```python
import os
import sentry_sdk

sentry_sdk.init(
    dsn=os.environ.get('TIDE_SENTRY_DSN'),
    environment=os.environ.get('ENV', 'dev'),
    release=f"tide@{os.environ.get('TIDE_VERSION', 'dev')}",
    traces_sample_rate=0.0,
    profiles_sample_rate=0.0,
    send_default_pii=False,
)
```

## React/Vite Frontend 接入

```typescript
// tide/frontend/src/main.tsx
import * as Sentry from '@sentry/react';

Sentry.init({
    dsn: import.meta.env.VITE_TIDE_SENTRY_DSN,
    environment: import.meta.env.VITE_ENV || 'dev',
    release: `tide@${import.meta.env.VITE_TIDE_VERSION}`,
    tracesSampleRate: 0.0,
    sendDefaultPii: false,
});
```

`.env.local`：

```
VITE_TIDE_SENTRY_DSN=https://<key>@aegis.kanpan.co/api/<project_id>/envelope/
VITE_ENV=dev
VITE_TIDE_VERSION=dev
```

## 关键监控点

| 监控点 | 严重度 |
|---|---|
| 券商 API 数据加载失败 | P0 |
| Tide v2 强决策推荐计算错 | P1 |
| 自动告警链失败（Tide v2） | P1 |
| 移动端 touch/滑动手势 error（frontend） | P2 |
| 图表渲染崩溃（frontend） | P2 |

## 量级预期

~100-300 events/day（用户面前端 + 后端）。

## 接入坐标（Wiki 确认后填）

- Python 入口: `<待经理人确认>`
- Frontend 入口: `tide/frontend/src/main.tsx`（待确认）
