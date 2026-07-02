# Aegis 生产部署指南

## 前置条件

- Docker + Docker Compose v2
- `helios-net` 已创建：`docker network create helios-net`
- `platform-postgres` 已在 helios-net 运行，数据库 `aegis` 已创建
- `helios-redis` 已在 helios-net 运行
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
- `CLOUDFLARED_TOKEN`
- `AEGIS_DOCKER_GID`（后端以非 root 运行，需 docker.sock 的属组：`stat -c '%g' /var/run/docker.sock`）

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
  --build-arg NEXT_PUBLIC_AEGIS_API=https://aegis.uex.hk \
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

# 测试 API
curl http://localhost/api/v1/health
```

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

### 查看日志

```bash
docker logs aegis-backend -f
docker logs aegis-console -f
docker logs aegis-caddy -f
```

### 数据备份

持久化数据在 Docker volume `aegis-data`，挂载到容器 `/data/aegis`：

```bash
# 导出 volume
docker run --rm \
  -v aegis_aegis-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/aegis-data-$(date +%Y%m%d).tar.gz -C /data .
```

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
| Redis 连接失败 | `docker exec aegis-backend python -c "import redis; redis.from_url('redis://helios-redis:6379/2').ping()"` |
