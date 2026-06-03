# Aegis Prod 部署指南 (M1)

经理人 SRE platform 范围内, Wiki 跟随此指南完成主机 ops 操作.

## 0. 前置

- Wiki 主机 (跟 platform-postgres 同主机), Docker + Docker Compose
- helios-net external network 已存在 (platform stack ship 时建)
- uex.hk Cloudflare 管理 (灰云 DNS only)

## 1. DNS 配置 (Cloudflare)

加 A 记录:
- 名: aegis
- 内容: <Wiki 主机 IPv4>
- Proxy: 灰云 (DNS only)
- TTL: Auto

验证: `dig +short aegis.uex.hk` 应返主机 IP.

## 2. 准备 env vars

```bash
cd ~/projects/aegis
cp env.aegis.example .env.aegis
# 编辑 .env.aegis 填真值:
# - POSTGRES_PASSWORD (从 platform .env 抄)
# - AEGIS_JWT_SECRET (openssl rand -hex 32)
# - AEGIS_SENTRY_DSN 暂留空, 后续步骤 seed
```

## 3. Build Aegis images

Build 时需 SSH key 装主库 git+ssh deps:

```bash
cd ~/projects/aegis

# Backend
docker build -f Dockerfile.prod -t aegis-backend:latest --ssh default .

# Console (在 aegis-console 子目录)
cd aegis-console
docker build -f Dockerfile.prod -t aegis-console:latest \
    --build-arg NEXT_PUBLIC_API_BASE_URL=https://aegis.uex.hk .
cd ..
```

注: helios-blocks tarball 必须在 console build context 内可达 (aegis-console 仓库内或挂入).

## 4. 启 Aegis stack

```bash
cd ~/projects/aegis
docker compose -f docker-compose.aegis.yml --env-file .env.aegis up -d
```

验证:
```bash
docker compose -f docker-compose.aegis.yml ps
# 3 service 都 running:
# - aegis-backend (healthy)
# - aegis-console (healthy)
# - aegis-caddy (running, port 80/443/443udp)
```

## 5. 等 Caddy LE 证书

```bash
docker compose -f docker-compose.aegis.yml logs aegis-caddy | grep -i "certificate"
# 等 5-30 分钟, 应见 "obtained certificate" 字样
```

## 6. 验证浏览器可达

- https://aegis.uex.hk → console 加载
- https://aegis.uex.hk/api/health → backend healthcheck 通过

## 7. Seed Aegis 自监控 DSN

```bash
# 7.1 用 console UI 创 org + project
# 浏览器 https://aegis.uex.hk
# 创 org "aegis-internal" + project "aegis-self"

# 7.2 psql 查 sentry_public_key
docker exec platform-postgres psql -U postgres -d aegis -c \
    "SELECT id, sentry_public_key FROM projects WHERE slug = 'aegis-self';"

# 7.3 拼 DSN 改 .env.aegis
# DSN 格式: https://<sentry_public_key>@aegis.uex.hk/api/<id>/envelope/
# 编辑 .env.aegis: AEGIS_SENTRY_DSN=https://<key>@aegis.uex.hk/api/<id>/envelope/

# 7.4 重启 aegis-backend 让 sentry init 生效
docker compose -f docker-compose.aegis.yml --env-file .env.aegis up -d aegis-backend
```

## 8. 完成

- ✅ https://aegis.uex.hk 公网可访问
- ✅ TLS 自动 LE 通过
- ✅ Aegis 自监控启用 (业务异常会自己上报到 Aegis)
- ✅ Wiki 可登录 console, 按 docs/M1_TRIAL.md 试用

## 已知限制 (M1)

- ❌ 无 errors UI (M2-D ship 后有)
- ❌ 无 DSN 显示 UI (M2-D ship 后有)
- ❌ 无 multi-tenant (Wiki 单用户)
- ❌ build 需 Wiki 主机直接装 (无 CI, M2 评估 GitHub Actions)
- ❌ Caddy LE 证书首次申请需 DNS 已生效 + 80 端口开

## 排错

| 问题 | 排查 |
|---|---|
| Caddy LE 证书获取失败 | DNS 生效 verify (dig), 80 端口 firewall verify |
| backend 启动报 platform-postgres 连不上 | helios-net external 存在 verify (docker network ls), POSTGRES_PASSWORD verify |
| console 502 | aegis-console 容器 logs verify, NEXT_PUBLIC_API_BASE_URL build-time verify |
| 自监控不上报 | AEGIS_SENTRY_DSN env var verify, aegis-backend logs grep sentry verify |
