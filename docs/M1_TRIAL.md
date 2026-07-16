# Aegis M1 试用指南

## 1. 登录 (https://aegis.kanpan.co)
- Aegis JWT auth (邮箱 + 密码)
- 默认 admin 账号: M2-E-Batch-2 seed

## 2. 创 Org + Project
- /orgs/new → 创 org
- /orgs/<slug>/projects/new → 创 project

## 3. 接 Webhook
- /orgs/<slug>/webhooks/new → 选 event_type (含 error.new_issue + error.spike)
- target URL: e.g. https://discord-webhook 或 https://requestbin

## 4. 获取 DSN
- M1 console 不显示, psql 查:
  docker exec platform-postgres psql -U <user> -d aegis -c \
    "SELECT id, sentry_public_key FROM projects WHERE slug = '<slug>';"
- DSN: https://<key>@aegis.kanpan.co/api/<id>/envelope/

## 5. 业务方接 SDK
- 按 docs/sdk-integration/ 对应文件
- sentry_sdk.init(dsn=<上一步>)

## 6. 触发测试错误
- 业务方代码 raise ValueError("test")

## 7. 查 errors (M1 用 psql, M2-D 加 UI)
- SELECT event_id, exception_type, ts FROM error_events WHERE project_id = '<id>' ORDER BY ts DESC LIMIT 10;
- SELECT issue_id, title, event_count, last_seen FROM error_issues WHERE project_id = '<id>' ORDER BY last_seen DESC;

## 8. 验证 webhook 投递
- /orgs/<slug>/webhooks → 看 delivery 记录

## 9. Aegis 自监控 (高级)

Aegis prod 默认启用自监控 (AEGIS_SENTRY_ENABLED=true). 当 Aegis 自己代码异常时,
错误会上报到 Aegis 自己的 project "aegis-self".

DSN seed 流程 + 启用步骤参考: docs/PROD_DEPLOY.md §7.

Wiki 可查 Aegis 内部错误: psql + WHERE project_id = '<aegis-self id>'.

## 已知限制 (M1)
- ❌ 无 errors list / detail UI (M2-D ship 后有)
- ❌ 无 DSN 显示 UI (M2-D ship 后有)
- ❌ 无 multi-tenant
- ❌ 无 OAuth2 / SSO
