# Aegis 部署指南

## 1. 前置依赖

Aegis 主服务依赖 4 个独立 repo:
- `obase` (3O 范式 base 层)
- `oprim` 主库 (含 oskill / omodul 三层)
- `aegis-autoheal-sdk`

这些 repo 不在 pypi 上, 必须本地 install 时显式指向。

## 2. 标准 install (推荐, 自部署)

假设所有 repo 在 `~/projects/` 下:

```bash
cd ~/projects/aegis
python -m venv .venv
.venv/bin/pip install -e ~/projects/obase
.venv/bin/pip install -e ~/projects/platform/oprim
.venv/bin/pip install -e ~/projects/aegis-autoheal-sdk
.venv/bin/pip install -e ".[test]"
```

## 3. 验证 install

```bash
.venv/bin/python -c "import obase, oprim, oskill, omodul, aegis_autoheal_sdk; print('all OK')"
.venv/bin/aegis --help
```

预期最后一行: `all OK`.

## 4. 数据库

Aegis 需要 Postgres 14+. 推荐 TimescaleDB.

```bash
# 设环境变量
export AEGIS_POSTGRES_DSN="postgresql://aegis:aegis@host:5432/aegis"

# 跑 migration (会自动 seed self-hosted 默认 org/project)
.venv/bin/aegis migrate
```

预期输出: `applied 4 migrations` (首次运行).

## 5. 启动

```bash
.venv/bin/aegis serve
# 默认 0.0.0.0:8080
```

健康检查:
```bash
curl http://localhost:8080/health   # 应返回 {"status":"ok"...}
curl http://localhost:8080/ready    # 应返回 {"status":"ready","db":"ok"}
```

## 6. 目录默认值

| 配置项 | 默认值 | 环境变量 |
|---|---|---|
| 数据目录 | `~/.aegis` | `AEGIS_DATA_DIR` |
| 日志目录 | `$AEGIS_DATA_DIR/logs` | `AEGIS_LOG_DIR` |
| Caddy 配置目录 | `$AEGIS_DATA_DIR/caddy` | `AEGIS_CADDY_CONFIG_DIR` |
| CORS 允许来源 | `http://localhost:3010` | `AEGIS_CORS_ORIGINS` |

所有目录在 `aegis serve` 启动时自动创建。不需要 root 权限。

`AEGIS_CORS_ORIGINS` 支持逗号分隔多个来源,生产部署时改为实际 console 域名,例如:
```bash
export AEGIS_CORS_ORIGINS="https://console.example.com"
```

## 7. 常见问题

### Q: install_app 后台 fail, 但前端只显示 status=failed 看不到原因?

A: 看 `${AEGIS_DATA_DIR:-~/.aegis}/logs/aegis.log` (后台任务也写到这里). 或重启 aegis 主服务时加 `--log-level debug`.

### Q: `ModuleNotFoundError: No module named 'omodul' / 'obase'`?

A: §2 的 pip install -e 没全跑. 重新执行 §2 那 4 行.

### Q: `ForeignKeyViolationError: ... violates foreign key constraint "installed_apps_org_id_fkey"`?

A: migration 004 没跑. 重新执行 `.venv/bin/aegis migrate`. 如果还有问题, 手动 seed:

```bash
docker exec -i <postgres-container> psql -U aegis -d aegis <<'EOF'
INSERT INTO orgs (id, name, plan) VALUES ('00000000-0000-0000-0000-000000000001', 'default', 'enterprise') ON CONFLICT DO NOTHING;
INSERT INTO projects (id, org_id, name, environment) VALUES ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'default', 'prod') ON CONFLICT DO NOTHING;
EOF
```
