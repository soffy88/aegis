# Python 项目接入模板

适用：Helivex / Selene / Stratum / Hevi（后端为主项目）。

## 接入代码

```python
import os
import sentry_sdk

PROJECT_NAME = "helivex"  # 替换为项目名

sentry_sdk.init(
    dsn=os.environ.get(f'{PROJECT_NAME.upper()}_SENTRY_DSN'),
    environment=os.environ.get('ENV', 'dev'),
    release=f"{PROJECT_NAME}@{os.environ.get(f'{PROJECT_NAME.upper()}_VERSION', 'dev')}",
    traces_sample_rate=0.0,
    profiles_sample_rate=0.0,
    send_default_pii=False,
)
```

替换 `PROJECT_NAME` 为 `helivex` / `selene` / `stratum` / `hevi`。

## DSN 获取

```sql
SELECT id, sentry_public_key FROM projects WHERE slug = '<project_slug>';
```

## 量级预期

各项目 ~30-100 events/day。

## 测试触发（任意项目通用）

```bash
python -c "
import os, sentry_sdk
sentry_sdk.init(dsn=os.environ['${PROJECT_NAME^^}_SENTRY_DSN'])
sentry_sdk.capture_exception(ValueError('test trigger from ${PROJECT_NAME}'))
sentry_sdk.flush(timeout=5)
"
```

## 验证

```sql
SELECT count(*) FROM error_events
WHERE project_id = '<your-project-id>'
  AND exception_value LIKE '%test trigger%'
  AND ts > NOW() - INTERVAL '5 minutes';
```

期望 ≥ 1。
