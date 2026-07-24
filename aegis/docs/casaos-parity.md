# CasaOS 能力对齐计划(用 3O 范式集成)

> 目标:把 CasaOS(github.com/IceWhaleTech/CasaOS)有、Aegis 还没有的能力,按 3O 范式集成进来。
> 决策(用户 2026-07-24):**全量对齐,override DESIGN.md §0.3/I9 非目标**(含驱动器挂载/格式化、宿主电源等宿主变更类)。四簇全做。
> 本文件是这个跨 `platform/3O` + `aegis` 双仓多轮工程的**权威进度骨架**(仿 `3o-new-elements.md`/`3o-v3-migration.md` 习惯)。

## 差距结论(盘点自 CasaOS-* 各仓 vs aegis 现状)

Aegis 在可观测/告警/自愈/多主机/RBAC/Docker 深度上远超 CasaOS。差距集中在 CasaOS 的"家用 NAS 家电面":
物理存储管理、文件预览/分享、网状 VPN、多 provider DDNS、宿主电源、持久 Web 终端。

## 工程约束(必须遵守)

- **3O 落库流程**:一元素一分支 `feat/<element>` → PR → 主库 Owner(经 CC)评审合入。依赖序 **obase → oprim → oskill → oservi → omodul**(底层先)。见 [[aegis-3o-extension-workflow]]。
- **⚠️ oprim 有并发编辑者**(worktree `oprim-tide-p1`、`oprim-pr19`,主 worktree 在 `integrate/helivex-3o-stack`)。**新元素必须在基于 `origin/main` 的隔离 git worktree 里做**,绝不在共享 worktree `git checkout`。
- **元素范式**:`oprim/_<name>.py`,`from __future__ import annotations`,Pydantic 返回模型,函数 `*,` keyword-only,中文 docstring,抛 `oprim._exceptions` 的 `OprimError` 族。`__init__` AST 自动导出非下划线名(加函数即可 `from oprim import <name>`)。
- **风险分级**(DESIGN §5):R0 只读 / R1 幂等可逆 / R2 高危(默认 dry-run + owner-only + 二次确认 + 写审计)/ R3 不可逆(移出自愈,仅人工 break-glass)。宿主变更类(mount/format/reboot)按 **R2/R3** 接入,不无脑执行——这是"override 非目标"的落地纪律。
- **测试**:oprim `uv run --extra dev [--with docker] pytest tests/xxx -q`;提交前 `git checkout uv.lock`。obase 同理。oskill 需 editable venv(uv 解析坑)。
- **aegis 侧**:新 router 挂 `aegis/server/api/routers/`,RBAC 门禁,危险操作走 `Permission` + owner 校验;需要的话加 migration;console 加页。

---

## 簇 1 — 存储/驱动器(CasaOS-LocalStorage 对齐)· 最高优先

### 3O 元素(oprim,除非注明)
| 元素 | 文件 | 签名要点 | 风险 | 状态 |
|---|---|---|---|---|
| block_device_list | `_block_device_list.py` | `lsblk -J -b` 解析 → 盘/分区树(name/size/type/fstype/mountpoint/model/serial/rota/tran) | R0 | ✅ 本地(feat/storage-observe,待 PR) |
| disk_smart_probe | `_disk_smart_probe.py` | `smartctl -j -H -A -i <dev>` → 健康(passed)+ 关键属性(温度/重映射/通电时长),ATA+NVMe,缺失优雅降级 | R0 | ✅ 本地(同上) |
| usb_device_list | `_usb_device_list.py` | `lsusb` → USB 设备枚举(vendor/product/root-hub 剔除) | R0 | ✅ 本地(同上) |
| partition_table_read | `_partition_table_read.py` | `lsblk`/`blkid` → 单盘分区+文件系统+UUID | R0 | ⬜ |
| filesystem_mount | `_filesystem_mount.py` | `mount`(可选 fstab 持久化)· **默认 dry_run** | **R2** | ⬜ |
| filesystem_unmount | `_filesystem_unmount.py` | `umount`(busy 检测)· 默认 dry_run | **R2** | ⬜ |
| filesystem_format | `_filesystem_format.py` | `mkfs.<ext4/xfs/btrfs>` · **R3,强制 dry_run 默认,确认令牌** | **R3** | ⬜ |
| storage_pool_plan | `_storage_pool_plan.py`(无状态) | MergerFS/mdadm 配置生成(只出计划,不执行) | R0 | ⬜ |

### aegis 接线
- router `routers/storage.py`:`GET /storage/devices`(block+smart+usb 聚合,viewer+)、`GET /storage/devices/{name}`、`POST /storage/mount`|`/unmount`(owner-only,dry_run 默认,二次确认)、`POST /storage/format`(owner-only,R3,确认令牌+审计)。
- service `services/storage.py`:聚合 + 缓存 + 写 `audit_log`/change-event。
- console `/storage` 页:盘卡片(容量环 + SMART 徽章 + USB 标记),挂载/格式化走危险操作二次确认弹窗。
- 复用:oprim `disk_usage`(已有)做用量环。

---

## 簇 2 — 文件管理增强(CasaOS 文件管理器对齐)

现有:`routers/files.py` + `services/files.py`(浏览/编辑/上传/下载/打包,沙箱 `AEGIS_FILE_MANAGER_ROOTS`)。缺预览/分享/网络盘。

### 3O 元素
| 元素 | 库/文件 | 签名要点 | 风险 | 状态 |
|---|---|---|---|---|
| thumbnail_generate | oskill `_thumbnail_generate.py` | 复用 obase `ffmpeg` + oprim `file_type_detector`;图/视频→缩略图(尺寸可注入) | R0 | ⬜ |
| media_probe | oprim `_media_probe.py` | `ffprobe` → 时长/编解码/分辨率(视频/音频预览元数据) | R0 | ⬜ |
| samba_share_apply | oprim `_samba_share_apply.py` | 生成/写 smb.conf 段 + 重载 · 默认 dry_run | **R2** | ⬜ |
| rclone_mount | oprim `_rclone_mount.py` | rclone remote 挂载(云盘作为文件位置)· 幂等 | R1 | ⬜ |

### aegis 接线
- files.py 加:`GET /files/thumbnail`(签名短 URL)、`GET /files/preview`(媒体元数据)、`POST /files/share`(生成限时签名分享令牌,存 `file_shares` 表 → migration)、`GET /s/{token}`(公开下载,免鉴权按令牌)。
- SMB/rclone 作为"存储位置"接入文件管理器根(与簇 1 挂载点打通)。
- console `/files`:缩略图网格、预览抽屉、分享弹窗(复制链接 + 有效期)。

---

## 簇 3 — 网状 VPN + 多 provider DDNS(远程接入对齐)

现有:仅 Cloudflare 隧道(`publish.py`/`services/cloudflare.py`)+ Cloudflare DNS。

### 3O 元素(oprim)
| 元素 | 文件 | 签名要点 | 风险 | 状态 |
|---|---|---|---|---|
| tailscale_up | `_tailscale_up.py` | `tailscale up --authkey`(幂等)+ 返回 tailnet IP | R1 | ⬜ |
| tailscale_status | `_tailscale_status.py` | `tailscale status -json` → 节点/在线/IP | R0 | ⬜ |
| zerotier_join | `_zerotier_join.py` | `zerotier-cli join <network>` + 状态 | R1 | ⬜ |
| ddns_update | `_ddns_update.py` | 多 provider(DuckDNS/No-IP/dyndns2 协议)→ 更新 A 记录,统一返回 | R1 | ⬜ |

### aegis 接线
- router `routers/remote_access.py`:VPN 接入(owner-only,authkey 走 secrets_vault)、状态、DDNS 配置 CRUD + 定时刷新(接 cron 循环)。
- service `services/remote_access.py`;secrets 存 authkey/token。
- console `/settings/remote-access` 页。

---

## 簇 4 — 持久 Web 终端(PTY)

现有:`docker exec`(admin+)、host-shell break-glass(owner-only 请求/响应式),无持久 PTY 流。

### 落点(主要 aegis/console,3O 侧薄)
| 元素 | 库/文件 | 要点 | 风险 | 状态 |
|---|---|---|---|---|
| pty_spawn_plan | oprim `_pty_spawn_plan.py`(无状态) | 出 exec/pty 启动参数(容器 vs 宿主 break-glass 容器),不持 fd | R0 | ⬜ |
| — | aegis `routers/terminal.py` | **WebSocket** PTY:后端 `pty.openpty()` + asyncio 桥接;宿主终端复用 owner-only break-glass 容器;容器终端复用 docker exec | **R2** | ⬜ |
| — | console `/host-terminal`,容器终端 | xterm.js 接 WS(现有页面从请求/响应升级为流式) | — | ⬜ |

---

## 阶段顺序(先地基、先只读、先低风险)

- **Phase 1**(✅ 三原语本地完成,23 tests 绿/ruff 清,分支 `feat/storage-observe` @ oprim 隔离 worktree,待 push+PR;`partition_table_read` 可直接从 block_device_list 的 children 派生,暂缓单列):`block_device_list` / `disk_smart_probe` / `usb_device_list`。
- **Phase 2**:aegis `routers/storage.py` + service + console `/storage`(只读盘视图)。
- **Phase 3**:簇 2 只读(thumbnail/media_probe + files 预览/分享)。
- **Phase 4**:簇 3 VPN + DDNS(R1)。
- **Phase 5**:簇 4 WebSocket PTY。
- **Phase 6**(最后、最高危):簇 1 R2/R3 宿主变更(mount/unmount/format)+ 簇 2 samba/rclone + 宿主电源。全部 dry_run 默认 + owner-only + 审计。DESIGN.md 需同步加一节说明 override。

## 待主库定(每 PR body 标注)
- 元素命名(`block_device_list` 等)、风险默认值(mount 默认 dry_run)、smartctl/lsblk 未安装时的降级返回契约。
- 宿主变更类是否进 oprim 主库(通用)还是 aegis 专有层——倾向进 oprim 但默认 dry_run,执行策略留 aegis。

## 参考
- 盘点来源:CasaOS-{Core,AppManagement,AppStore,LocalStorage,UserService,Gateway,MessageBus,JobManagement}。
- 相关记忆:[[aegis-3o-extension-workflow]]、[[aegis-3o-elements-progress]]、[[aegis-appstore-install-flow]]、[[aegis-caddy-edge-routing]]。
