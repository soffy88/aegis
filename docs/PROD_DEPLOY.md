# Aegis Prod 部署指南 (M1, Cloudflare Tunnel)

经理人 SRE platform 范围内, Wiki 跟随此指南完成主机 ops 操作.

## 0. 前置

- Wiki 主机 (跟 platform-postgres 同主机), Docker + Docker Compose
- helios-net external network 已存在
- uex.hk Cloudflare 管理 (Zero Trust 启用)
- Wiki 主机**不需要**固定 IP / 不需要 80/443 端口入站 (Cloudflare Tunnel 反向连)

## 1. Cloudflare 创 Tunnel (Wiki Cloudflare 控制台)

1.1 登录 Cloudflare → Zero Trust (free plan OK)
1.2 Networks → Tunnels → Create a tunnel
1.3 选 Cloudflared (不是 WARP), 命名 "aegis-prod"
1.4 复制 token (`eyJh...` 长字符串)
1.5 **不要** 跟 Cloudflare 默认 install 走完, 跳到 next step "Connectors" 留空 (本机 docker 跑 cloudflared)
1.6 进 "Public hostnames" 配 ingress:
    - Subdomain: aegis
    - Domain: uex.hk
    - Type: HTTP
    - URL: aegis-caddy:8080
1.7 Save
1.8 Cloudflare 自动加 DNS CNAME: aegis.uex.hk → <tunnel-id>.cfargotunnel.com (Wiki 不用手动加)

## 2. 准备 env vars

```bash
cd ~/projects/aegis
cp env.aegis.example .env.aegis
# 编辑 .env.aegis 填真值:
# - POSTGRES_PASSWORD (从 platform .env 抄)
# - AEGIS_JWT_SECRET (openssl rand -hex 32)
# - CLOUDFLARED_TOKEN (Step 1.4 复制)
# - AEGIS_SENTRY_DSN 暂留空 (Step 7 seed)
```

## 3. Build Aegis images

```bash
cd ~/projects/aegis

# Backend
docker build -f Dockerfile.prod -t aegis-backend:latest --ssh default .

# Console (aegis-console 子目录)
cd aegis-console
docker build -f Dockerfile.prod -t aegis-console:latest \
    --build-arg NEXT_PUBLIC_API_BASE_URL=https://aegis.uex.hk .
cd ..
```

注: helios-blocks tarball 必须在 console build context 可达.

## 4. 启 Aegis stack

```bash
cd ~/projects/aegis
docker compose -f docker-compose.aegis.yml --env-file .env.aegis up -d
```

验证:
```bash
docker compose -f docker-compose.aegis.yml ps
# 4 service running:
# - aegis-backend (healthy)
# - aegis-console (healthy)
# - aegis-caddy (running, 不 publish 端口)
# - aegis-cloudflared (running, 反向连 CF)
```

## 5. 等 Cloudflare Tunnel 连接

```bash
docker compose -f docker-compose.aegis.yml logs aegis-cloudflared | tail -10
# 应见 "Registered tunnel connection" 字样, 通常 5-30 秒
```

Cloudflare 控制台 Tunnels 页 → aegis-prod tunnel 状态从 "Inactive" 变 "Healthy" (绿).

## 6. 验证浏览器可达

- https://aegis.uex.hk → console 加载 (TLS 由 CF 边缘自动)
- https://aegis.uex.hk/api/health → backend healthcheck 通过

注: DNS CNAME 生效需 5-30 分钟 (Cloudflare 通常很快), `dig aegis.uex.hk` 应返 `<tunnel-id>.cfargotunnel.com`.

## 7. Seed Aegis 自监控 DSN

```bash
# 7.1 console UI 创 org "aegis-internal" + project "aegis-self"
# 浏览器 https://aegis.uex.hk

# 7.2 psql 查 sentry_public_key
docker exec platform-postgres psql -U postgres -d aegis -c \
    "SELECT id, sentry_public_key FROM projects WHERE slug = 'aegis-self';"

# 7.3 拼 DSN 改 .env.aegis
# DSN 格式: https://<sentry_public_key>@aegis.uex.hk/api/<id>/envelope/
# 编辑 .env.aegis: AEGIS_SENTRY_DSN=https://<key>@aegis.uex.hk/api/<id>/envelope/

# 7.4 重启 aegis-backend
docker compose -f docker-compose.aegis.yml --env-file .env.aegis up -d aegis-backend
```

## 8. 完成

- ✅ https://aegis.uex.hk 公网可访问 (Cloudflare Tunnel + CF 边缘 TLS)
- ✅ Wiki 主机动态 IP 0 影响 (反向连接)
- ✅ Aegis 自监控启用
- ✅ Wiki 可登录 console, 按 docs/M1_TRIAL.md 试用

## 9. 已知限制 + 排错

### 限制 (M1)
- ❌ 无 errors UI (M2-D ship 后有)
- ❌ 无 DSN 显示 UI (M2-D ship 后有)
- ❌ 无 multi-tenant
- ❌ Cloudflare free plan 限制 (50/无限带宽 / etc., M1 阶段不会触)

### 排错

| 问题 | 排查 |
|---|---|
| Cloudflare Tunnel 没连上 | `docker logs aegis-cloudflared`, verify CLOUDFLARED_TOKEN 正确, CF 控制台 tunnel 状态 |
| aegis.uex.hk 502 | aegis-caddy logs, verify Caddyfile :8080 内部反代正常 |
| aegis.uex.hk 加载但报 CORS | backend AEGIS_CORS_ORIGINS=https://aegis.uex.hk verify |
| backend 启动报 platform-postgres 连不上 | helios-net external 存在 (docker network ls), POSTGRES_PASSWORD verify |
| console 502 | aegis-console 容器 logs, NEXT_PUBLIC_API_BASE_URL build-time verify |
| 自监控不上报 | AEGIS_SENTRY_DSN env var verify, aegis-backend logs grep sentry verify |

### M3+ 扩展 (Aegis 给其他项目公网发布)

加新项目 (e.g. helios):
1. Caddyfile 加新 server block `:8081 { ... }`
2. docker-compose.aegis.yml 加项目 service (e.g. helios-backend)
3. CF 控制台 tunnel ingress 加 `helios.uex.hk → aegis-caddy:8081`
4. 无需 0 改 Aegis 自身 / 0 改 backend / 0 跨主机
