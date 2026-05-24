# C0 完成时给 Wiki 的反馈 (2026-05-24)

Aegis 经理人收集, C0c/C0d 实施过程中暴露的 4 个主库 vs SPEC 差异.
不是责问, 是双 owner 制下的健康同步.

## 1. oprim.docker_container_list 已存在

Step 12 SPEC 中 docker 类 oprim 列了 7 个 (inspect/logs/start/stop/restart/image_pull/stats),
未含 list. 实际 oprim 2.11.0 已 export `docker_container_list(all, filters, docker_host)`.

**好消息**: 主库比 SPEC 更完整. 建议: 下次更新 Step 12 SPEC 文档时补上.

## 2. oprim.OprimError 未顶层 export

Aegis 在 C0c 实施时为 catch oprim 异常, 被迫 `from oprim._exceptions import OprimError`
(下划线开头是私有子模块, Aegis 不应该 import 这种).

**建议**: 主库 PATCH bump 把 OprimError 顶层 export, 即 `from oprim import OprimError` 可用.

## 3. obase.ProviderRegistry 实际签名跟 Step 12 SPEC 不一致

Step 12 SPEC 写的伪签名:
  `ProviderRegistry.register(name=..., api_key=...)`
  `ProviderRegistry.get(name).create_caller(model=...)`

主库实际签名:
  `ProviderRegistry.register(category: str, name: str, fn: Callable, replace: bool)`
  `ProviderRegistry.get_caller(provider: str, model: str) -> Callable`

**主库实际更通用** (category 维度 + fn 注入). Step 12 SPEC 写得过简化.

**建议**: SPEC 文档下次更新时补 ProviderRegistry 真签名 + caller factory pattern example.

## 4. omodul 1.10 Requires-Dist 实际是 oskill>=3.0

Wiki 2026-05-24 早期沟通时说 "omodul 1.10 还依赖 oskill<3.0", 但实际 omodul 1.10.0 的
Requires-Dist 是 `oskill>=3.0`, 与 Aegis 装的 oskill 3.0.0 完全兼容 (pip check 干净).

可能 Wiki 当时记错版本. 不影响 Aegis 进度, 但下次跨 owner 沟通时建议交叉验证 metadata.

## 5. (Aegis 端 backlog, 等主库 PATCH)

- omodul `__version__` 缺失 — Aegis 暂用 hasattr 包容; 主库 PATCH 后取消
  `test_omodul_exposes_version` 的 xfail
