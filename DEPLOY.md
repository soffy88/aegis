# Aegis 生产部署指南

## 前置条件

- Docker + Docker Compose v2
- `helios-net` 已创建：`docker network create helios-net`
- `platform-postgres` 已在 helios-net 运行，数据库 `aegis` 已创建
- `infisical-redis` 已在 helios-net 运行（compose 里 `AEGIS_REDIS_URL` 指向它）
- Cloudflare Tunnel token 已准备好

## 首次部署

### 1. 准备配置

```bash
cp env.aegis.example .env.aegis
# 编辑 .env.aegis，填入所有必填项
nano .env.aegis
```

必填项清单：
- `POSTGRES_PASSWORD`
- `AEGIS_JWT_SECRET`（`openssl rand -hex 32` 生成）
- `AEGIS_OLLAMA_GATEWAY_TOKEN`（`openssl rand -hex 32`；prod 下留空则 GPU/Ollama 网关 fail-closed 返回 503）
- `CLOUDFLARED_TOKEN`
- `AEGIS_DOCKER_GID`（后端以非 root 运行，需 docker.sock 的属组：`stat -c '%g' /var/run/docker.sock`）

> 建议同时设置 `AEGIS_SECRETS_MASTER_KEY`（独立 32 字节 key）——留空则金库密钥派生自
> `AEGIS_JWT_SECRET`，日后轮换 JWT 会孤立所有已加密的 org secrets。设定后请勿再改。

> 升级到非 root 镜像后，**已存在的 `aegis-data` volume 仍属 root**。一次性修正属主：
> ```bash
> docker run --rm -v aegis-data:/data alpine chown -R 10001:10001 /data
> ```
> 全新部署无需此步（fresh volume 会继承镜像内 `/data/aegis` 的 aegis 属主）。

### 2. 创建数据库

```bash
# 连接 platform-postgres 创建 aegis 数据库
docker exec -it platform-postgres psql -U helios -c "CREATE DATABASE aegis;"
```

### 3. 构建镜像

```bash
# 后端（需要 SSH key 访问私有依赖）
DOCKER_BUILDKIT=1 docker build \
  --ssh default \
  -f Dockerfile.prod \
  -t aegis-backend:latest .

# 前端（需 platform 构建上下文提供 OUI 私有包 tarball）
docker build \
  -f aegis-console/Dockerfile.prod \
  --build-context platform=/data/soffy/projects/platform \
  --build-arg NEXT_PUBLIC_AEGIS_API=https://aegis.kanpan.co \
  -t aegis-console:latest \
  aegis-console/
```

### 4. 启动服务

```bash
docker compose -f docker-compose.aegis.yml --env-file .env.aegis up -d
```

### 5. 验证

```bash
# 检查所有服务健康
docker compose -f docker-compose.aegis.yml ps

# 检查后端日志（迁移是否成功）
docker logs aegis-backend --tail 50

# 测试 API — 后端/caddy 端口都不发布到宿主，所以用以下两种之一：
# ① 端到端（经 Cloudflare 隧道 → caddy:8080 → backend）
curl https://aegis.kanpan.co/api/v1/health
# ② 本机内部（容器内直连后端，含 DB 就绪探针）
docker exec aegis-backend python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/v1/health/ready').read())"
```

> 拓扑：`cloudflared → aegis-caddy:8080 → /api/* 到 aegis-backend、其余到 aegis-console`。
> caddy 另发布 `:80/:443` 仅用于运行时动态添加的客户网站/域名路由（自动 HTTPS），
> **应用本身不在 :80**，因此 `curl http://localhost/...` 不会命中 API。

## 日常运维

### 更新部署（有 SSH key）

```bash
# 重新构建
DOCKER_BUILDKIT=1 docker build --ssh default -f Dockerfile.prod -t aegis-backend:latest .

# 滚动更新
docker compose -f docker-compose.aegis.yml --env-file .env.aegis up -d aegis-backend
```

### 热更新（无 SSH key，仅改源码）

```bash
# 用 hotpatch Dockerfile，复用已有 .venv
docker build -f Dockerfile.hotpatch -t aegis-backend:latest .
docker compose -f docker-compose.aegis.yml --env-file .env.aegis up -d aegis-backend
```

### 升级与回滚（重要）

- **数据库迁移是只进不退的**：`apply_migrations` 在启动时自动运行（advisory-lock 保护、
  逐条事务），但**没有 down 迁移**，且个别迁移含破坏性/不可重放操作。因此：
  **任何带新迁移的部署前，先给 aegis 库做一次快照**：
  ```bash
  docker exec platform-postgres pg_dump -U helios -d aegis -Fc -f /tmp/aegis-pre-deploy.dump
  docker cp platform-postgres:/tmp/aegis-pre-deploy.dump ./aegis-pre-deploy-$(date +%Y%m%d).dump
  ```
- **pin 镜像版本以便回滚**：生产不要用 `AEGIS_VERSION=latest`。用 git sha/tag 打标并写进
  `.env.aegis`（`AEGIS_VERSION=<sha>`），回滚就是把它改回上一版再 `up -d`。DB 侧因迁移
  只进不退，回滚镜像时若新版加过迁移，需要用上面的快照 restore。

### 查看日志

```bash
docker logs aegis-backend -f
docker logs aegis-console -f
docker logs aegis-caddy -f
```

**聚合日志（Loki + promtail）**：`aegis-promtail` 经 Docker 服务发现 tail 所有容器
stdout/stderr，打 `container`/`stack`/`service` 标签推到 `aegis-loki`（14 天保留，见
`loki-config.yaml`）。在 Console 的 **/loki** 页用 LogQL 查询，例：

```logql
{container="aegis-backend"}
{stack="aegis"} |= "error"
```

> 冷启动时 promtail 会尝试补推历史 json-file 日志，超过 7 天的会被 Loki 以
> `timestamp too old` 拒绝（无害，新日志正常入库）；读取偏移持久化在
> `aegis-promtail-positions` 卷，重启不会重读。
>
> ⚠️ 仍无独立错误收件箱：Sentry 在 prod 按设计关闭（避免自监控死循环），错误现经聚合
> 日志检索（`|= "ERROR"`）。

### 链路追踪（opt-in）

摄取 + 存储 + 查询 UI 已就绪：服务把 OTLP trace 发到 aegis，落 `aegis_spans`，在
Console 的 **APM / Service Map** 页看 per-service RED 与 trace 瀑布。缺的是采集前门与
各服务埋点，按需启用：

1. **启用 collector**（把各服务的标准 OTLP 翻译成摄取端点要的 OTLP/JSON）：
   ```bash
   # .env.aegis 设:
   #   AEGIS_TRACE_INGEST_ENDPOINT=http://aegis-backend:8000/api/v1/telemetry/<org uuid>/v1/traces
   #   AEGIS_TELEMETRY_INGEST_KEY=<与后端一致的 key>
   docker run --rm -v $PWD/otel-collector-config.yaml:/c.yaml:ro \
     otel/opentelemetry-collector:0.116.0 validate --config=/c.yaml   # 先校验
   docker compose -f docker-compose.aegis.yml --env-file .env.aegis --profile tracing up -d aegis-otel-collector
   ```
2. **让服务埋点**：把服务的 OpenTelemetry OTLP exporter 指向 collector
   （`OTEL_EXPORTER_OTLP_ENDPOINT=http://aegis-otel-collector:4318`，HTTP；或 `:4317` gRPC），
   `OTEL_SERVICE_NAME=<服务名>`。span 即经 collector → aegis 摄取 → APM 页。

> 说明：aegis 后端自身尚未内置 OTel 自动埋点（避免默认镜像引入 OTel 依赖）——推荐用上面
> 的标准 OTLP 环境变量给需要追踪的服务逐个开启，而不是手写 exporter。摄取端的 OTLP/JSON
> 契约有测试锁定（`test_telemetry_router.py`）；collector 配置为标准 otelcol schema，务必
> 用上面的 `validate` 在部署前校验一次。

### 自定义域名 / TLS

Console 的 **Domains** 页注册一个域名会经 Caddy admin API 建一条 org 命名空间的反代
路由（与 **/edge/routes** 同一套机制，STATUS #18 已收敛，不再依赖独立的 aegis-edge 服务）。
TLS 证书由 Caddy 自动经 ACME(Let's Encrypt) 签发——**前提是该域名的 DNS 已指向本机/隧道**。

- **DNS 是外部/注册商步骤**：在你的 DNS 服务商处把域名 A/CNAME 指到公网入口（或经
  Cloudflare 隧道加 CNAME）。aegis 不托管 DNS 记录。
- DNS 生效后 Caddy 自动签证书；**Certificates** 页实时探测 :443 上真实证书，回显
  issuer / 到期日 / 剩余天数（`< 21 天` 标记 expiring）。
- 删除域名会同时移除 Caddy 路由。

### 数据备份

持久化数据在 Docker volume `aegis-data`，挂载到容器 `/data/aegis`：

```bash
# 导出 volume
docker run --rm \
  -v aegis_aegis-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/aegis-data-$(date +%Y%m%d).tar.gz -C /data .
```

> ⚠️ **异地备份**：内置的 `self_backup`（pg_dump）默认落在**同一台主机的 aegis-data 卷**内，
> 宿主机丢失即备份一起丢；且应用级备份功能的 S3 上传目前仍是 omodul 桩，`backup_key` 未必
> 指向真对象。上线前请二选一配置真正的异地目标：
> - `AEGIS_BACKUP_S3_*` / `AWS_*`（S3/兼容对象存储），或 `AEGIS_BACKUP_WEBDAV_*`；
> - 或用上面的 volume-tar + 上一节的 `pg_dump` 做外部 cron，推到另一台主机/对象存储。
> 只配一个也比只落本机强。

## 配置 Runbook

Runbook YAML 文件放在 `/data/aegis/runbooks/`（volume 内）：

```bash
# 进入后端容器
docker exec -it aegis-backend bash

# 创建 runbook
cat > /data/aegis/runbooks/restart-nginx.yml << 'EOF'
name: restart-nginx
description: Restart nginx container when health check fails
trigger: container_unhealthy
requires_approval: false
steps:
  - name: restart container
    type: docker
    command: restart nginx
    timeout: 30
EOF
```

重启后端后 Runbook 自动加载并索引到向量库。

## 安全模型 / 信任边界（务必阅读）

本部署采用**共享主机（可信租户）模型**：

- **平台 Docker daemon 与主机指标是所有 org 共享的基础设施。** 平台主机（`node_id`
  省略时）上的容器列表、日志、指标对本部署内任一 org 的用户可见；容器操作只按**角色**
  分级，不按 org 归属细分。因此**只应接入你信任的 org**（自己/内部团队）。
- **越权高危动作已按角色收紧**：容器内 `exec` 与交互终端需 **admin+**；
  `host-shell`（特权 `-v /:/host` → 宿主机 root，break-glass）需 **owner**。
- **要隔离互不信任的租户**，给该租户注册**它自己的 node**（独立 docker_host_url）——
  node 上的操作按 `org_id` 严格作用域；不要让多个互不信任方共用平台 daemon。
- 若未来确需在共享 daemon 上做硬隔离，容器已打 `aegis.org`/`aegis.project` 标签，
  可据此加按标签的归属强制（当前信任模型下未启用）。

## 接入现有项目

1. 进入 Aegis Console → Nodes → 注册节点（本机填 `unix:///var/run/docker.sock`）
2. 进入 Containers 确认项目容器出现在列表
3. 进入 Alert Rules 为每个项目配置告警规则
4. 进入 Runbooks 为每个项目编写运维手册
5. 在 Brain 调试面板测试 Triage → RCA → ActionPlan 链路

## 故障排查

| 问题 | 排查命令 |
|---|---|
| 后端无法连 DB | `docker exec aegis-backend python -c "import asyncpg"` |
| 迁移失败 | `docker logs aegis-backend | grep migration` |
| Brain Agent 未初始化 | `curl /api/v1/orgs/{id}/brain/status` |
| 容器管理不可用 | 检查 `/var/run/docker.sock` 挂载是否存在 |
| Redis 连接失败 | `docker exec aegis-backend python -c "import redis; redis.from_url('redis://infisical-redis:6379/2').ping()"` |
