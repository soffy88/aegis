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
- ✅ router `routers/storage.py`(已注册 app.py):`GET /storage`(聚合 overview,含逐盘 SMART)、`GET /storage/devices`、`GET /storage/usb`、`GET /storage/smart?device=`。均 read-only,RBAC 门禁 **INSTALL_APP**(operator+,与 firewall 同——因走特权 host-shell helper)。
- ✅ service `services/storage.py`:在**宿主**经 helper 跑 lsblk/lsusb/smartctl → 交 oprim 原语解析(`lsblk_json=`/`lsusb_output=`/`smartctl_json=`);oprim 未 bump 时惰性导入降级为 503。逐盘 SMART best-effort、设备路径正则白名单 + shlex 转义防注入。
- ✅ service `services/host_shell.py`:抽出共享的特权宿主命令 helper(`host_exec`/`host_capture`/`sh_quote`),复用 `aegis-host-shell` 容器(未改 firewall.py)。
- ✅ 测试 `tests/test_storage_service.py`:解析路径(拷原语真跑,8 绿)+ 降级/校验(真 venv,2 绿/6 skip)。app 收集 1113 无破坏。
- ✅ console `/storage` 页:盘卡片(型号/容量 + NVMe-SATA-USB/HDD-SSD/SMART 健康/温度徽章 + 分区挂载点)+ USB 列表;503 降级横幅。导航挂 Infrastructure 段,i18n en+zh 齐,typecheck+lint 清。
- ⬜ 未做:挂载/格式化(Phase 6 R2/R3)。
- ⚠️ **依赖**:三原语在 oprim `feat/storage-observe`(未发版)。live 生效需:oprim PR 合并 → 发 tag(建议 v3.21.0)→ bump aegis oprim pin(pyproject 现 v3.19.0)。未 bump 前端点返回 503 明确原因,不崩。
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
- ✅ **分享链接(Phase 3a,已提交、可 live)**:migration `052_file_shares`(token 存 sha256、path/expires/max_downloads/download_count/revoked)、`services/file_shares.py`(create/list/revoke/resolve,解析时复验路径在 roots 内 + 查过期/限次,行锁原子自增)、files.py 加 `POST /files/share`(operator+)/`GET /files/shares`/`DELETE /files/shares/{id}`、**公开无鉴权 `GET /s/{token}`**(`routers/file_share_public.py`,失败一律 404 不做 oracle)。测试 `tests/test_file_shares.py` 12 绿、ruff 清、1125 收集。**纯 aegis、无 3O 发版依赖,部署即 live**(不像存储要等 oprim#22)。
- ✅ **缩略图(Phase 3b,已提交)**:oprim `thumbnail_generate`(Pillow image extra)**PR #24** + aegis `GET /files/thumbnail`(读沙箱图像→原语→webp,惰性导入降级 503,40MiB 上限,Cache-Control)。`tests/test_file_thumbnail.py` 4 绿/1 skip。**注:aegis 应依赖 `oprim[image]`;Pillow 已在 venv。**
- ⬜ 视频缩略图/媒体预览:需给 backend 镜像加 ffmpeg(现只 libpq5/libmagic1)+ oprim `media_probe`。
- ⬜ SMB/rclone 作为"存储位置"(Phase 6 R2/网络配置)。
- ⬜ console `/files`:分享弹窗(复制链接 + 有效期)、缩略图网格、预览抽屉。

---

## 簇 3 — 网状 VPN + 多 provider DDNS(远程接入对齐)

现有:仅 Cloudflare 隧道(`publish.py`/`services/cloudflare.py`)+ Cloudflare DNS。

### 3O 元素(oprim)
| 元素 | 文件 | 签名要点 | 风险 | 状态 |
|---|---|---|---|---|
| tailscale_status | `_tailscale_status.py` | `tailscale status --json` 解析 → running/self_ips/tailnet/peers,dual-mode,未装 installed=False | R0 | ✅ **PR #25**(6 tests) |
| tailscale_up | (aegis 侧 host_exec) | `tailscale up --authkey`(宿主网络变更,不单列 oprim 元素——动作无解析价值) | R2 | ✅ aegis |
| zerotier_join/status | `_zerotier_*.py` | zerotier-cli(需宿主 daemon) | R1/R2 | ⬜ 后续 |
| ddns_update | `_ddns_update.py` | 多 provider(DuckDNS/dyndns2=No-IP/DynDNS)→ 更新 A 记录,统一 DdnsResult;纯 httpx 无宿主依赖 | R1 | ✅ **PR helios-plat/oprim#23**(12 tests) |

### aegis 接线
- ✅ **DDNS(Phase 4a,已提交)**:migration `053_ddns_configs`(凭据不入表,存 vault `ddns:<id>:secret`)、`services/ddns.py`(create/list/delete/update_now,凭据走 secrets_vault,消费 oprim `ddns_update`,未 bump 降级 503)、`routers/remote_access.py`(POST/GET/DELETE `/remote-access/ddns` + POST `/{id}/update`;建/删 admin+,刷新 operator+)、注册 app.py。测试 `tests/test_ddns.py` 9 绿、ruff 清、1134 收集。
- ✅ **VPN Tailscale(Phase 4b,已提交)**:`services/vpn.py`(经 host-shell 在宿主取 `tailscale status --json` → oprim `tailscale_status` 解析;`tailscale up --authkey` 经 host_exec,R2)、`routers/remote_access.py` 加 `GET /vpn/tailscale`(viewer+)/`POST /vpn/tailscale/up`(admin+)。未装宿主返回 installed=False,oprim 未 bump 降级 503。`tests/test_vpn.py` 8 绿。
- ⬜ ZeroTier(需宿主 daemon,同模式);cron 定时刷新循环(现仅手动 update-now,循环触及受监督编排机制 + 未 bump 无法验证,推迟);console `/settings/remote-access` 页。

---

## 簇 4 — 持久 Web 终端(PTY)

现有:`docker exec`(admin+)、host-shell break-glass(owner-only 请求/响应式),无持久 PTY 流。

### 落点(主要 aegis/console,3O 侧薄)
| 元素 | 库/文件 | 要点 | 风险 | 状态 |
|---|---|---|---|---|
| — | 容器终端 WS | **已存在**:docker.py `/containers/{name}/terminal`(docker exec + asyncio 桥接,admin+) | R2 | ✅ 既有 |
| — | 宿主终端 WS(**Phase 5 已提交**) | docker.py `/host-terminal`:exec 进特权 helper `chroot /host bash` + asyncio 桥接;**owner-only**(比容器 admin 更强);`_ws_authorize` 校验 token/成员/角色。8 tests(鉴权门,PTY 桥需真 docker 无法此处验) | **R2** | ✅ aegis |
| — | console `/host-terminal` | xterm.js 接 WS(现有页面升级为流式) | — | ⬜ console |
| pty_spawn_plan | oprim(无状态) | 未做:exec/pty argv 构建对 WS 终端价值薄,直接 aegis 侧构建 | R0 | ⬜ 暂不做 |

---

## 阶段顺序(先地基、先只读、先低风险)

- **Phase 1**(✅ 三原语本地完成,23 tests 绿/ruff 清,分支 `feat/storage-observe` @ oprim 隔离 worktree,待 push+PR;`partition_table_read` 可直接从 block_device_list 的 children 派生,暂缓单列):`block_device_list` / `disk_smart_probe` / `usb_device_list`。
- **Phase 2**(✅ 后端+前端完成):aegis `routers/storage.py` + `services/storage.py` + `services/host_shell.py` + 测试(解析 8 绿 / 降级 2 绿,1113 收集无破坏,ruff 清)+ console `/storage` 页(typecheck+lint 清)。宿主经特权 helper 跑命令、oprim 解析、未 bump 降级 503。⬜ 仅剩 oprim PR #22 合并发版后 bump aegis pin 才 live。oprim 分支已 push,PR **helios-plat/oprim#22**。
- **Phase 3a**(✅ 已提交、部署即 live):文件分享链接(migration 052 + file_shares 服务 + `/files/share*` + 公开 `/s/{token}`,12 tests 绿)。纯 aegis 无 3O 发版依赖。⬜ console 分享弹窗待做。
- **Phase 3b**(✅ 图像缩略图已提交):oprim `thumbnail_generate` **PR #24** + aegis `/files/thumbnail`(4 tests)。⬜ 视频/媒体预览需镜像加 ffmpeg + `media_probe`。
- **Phase 4a**(✅ 已提交):DDNS 多 provider(oprim `ddns_update` **PR #23** + aegis migration 053 + `services/ddns.py` + `routers/remote_access.py`,9 tests 绿)。live 待 oprim#23 发版 + bump pin。
- **Phase 4b**(✅ Tailscale 已提交):oprim `tailscale_status` **PR #25** + aegis vpn service/端点(8 tests)。⬜ ZeroTier + cron 刷新 + console 页。
- **Phase 5**(✅ 宿主终端 WS 已提交、部署即 live):docker.py `/host-terminal` WebSocket(owner-only,exec 进特权 helper chroot /host,复用容器终端桥接模式)+ `_ws_authorize` + host_shell `ensure_helper`。8 tests(鉴权门)。⚠️ PTY 桥接需真 docker + 浏览器 xterm.js **无法此处端到端验证**。⬜ console `/host-terminal` 页接 WS。纯 aegis 无 3O 依赖。
- **Phase 5**:簇 4 WebSocket PTY。
- **Phase 6**(最后、最高危):簇 1 R2/R3 宿主变更(mount/unmount/format)+ 簇 2 samba/rclone + 宿主电源。全部 dry_run 默认 + owner-only + 审计。DESIGN.md 需同步加一节说明 override。

## ✅ 发版收口(D,2026-07-24)
- oprim 4 PR(#22 存储三原语 / #23 ddns_update / #24 thumbnail_generate / #25 tailscale_status)全部合入 main → **发版 v3.21.0**(经 PR #26 bump 版本 + tag,tag 指向 main 可达提交)。纯增量,无既有 API 变更。
- aegis oprim pin **v3.19.0 → v3.21.0**(commit `aede6ad`),主依赖改 `oprim[image]`(缩略图 Pillow)。
- **全 6 原语从固定版真导入 OK;存储/DDNS/VPN/缩略图端点脱离 503 降级、真正 live。**
- 全量套件 **988 passed / 166 skipped / 0 failed**,pin bump 零回归。部署 aegis-backend(重建镜像)后生效。
- 遗留(与本工程无关):`boto3` 未在 aegis 声明却被 oskill `restore_from_backup` 无条件 import(历史"侥幸装了"),建议后续正式声明。

## ✅ console 前端接线完成(aegis-console commit `b42d209`)
- `/storage`(盘/SMART/USB)、files 分享弹窗(→ /s/{token} 链接)+ 图片详情面板缩略图预览(鉴权 aegisBlob→objectURL,避开 img 无 Bearer)、`/settings/remote-access`(Tailscale 状态/接入 + DDNS CRUD/更新)、`/host-terminal` 改用 owner-only `/docker/host-terminal` WS(ContainerTerminal 加可选 wsPath)。nav + i18n(en+zh)齐,typecheck+lint 清。
- ⚠️ 未含别人未提交的 publish 页 + Dockerfile/package/pnpm(OUI)WIP;共享文件(AppFrame/api-paths/messages)含少量既有 publish 行(无 git add -p,连带)。console 从工作区部署,提交完整性不影响功能。

## ✅ 簇 6 危险宿主变更后端完成(部署即 live)
- **设计判断**:格式化磁盘/关宿主机 **不放进共享 oprim**(任何消费者能格式化盘=footgun),价值在安全策略层(aegis 职责,DESIGN §5)。故做在 aegis `services/storage_ops.py`,纯 aegis 无 oprim 发版依赖。
- mount/unmount(R2)、format(R3)、host_power(R3)。全部 **dry_run 默认 + owner-only(`require_min_role(OWNER)`)+ 审计**。护栏:挂载点白名单(/mnt、/media)、fstype 白名单(ext4/xfs/btrfs)、**format 确认令牌须等于设备路径 + 拒绝已挂载/系统盘(整盘检查)**、power 确认令牌须等于 action。经 host-shell + shlex 转义。
- `routers/storage.py` 加 POST /mount /unmount /format /power(owner-only)。`tests/test_storage_ops.py` 17 绿(护栏全覆盖),1172 收集。
- ✅ **console danger-zone**(aegis-console `4f939f8`):`/storage` 页底 `StorageManagement` 组件——挂载/格式化/宿主电源,dry-run 优先(先「预览」显示命令再「执行」),格式化/电源需确认回显匹配才可点,红框标注。

## ✅ 三项遗留全部收尾(2026-07-24)
- **DDNS cron 刷新**(aegis `c711d59`):`_ddns_refresh_loop`(5min)遍历 enabled ddns_configs → update_now,纳入 _SUPERVISED_LOOPS。
- **ZeroTier**(aegis `57c18dc` + console `741835b`):`services/vpn.py` zerotier_status/join(内联解析,非 oprim);remote-access 页 ZeroTier 区。
- **视频缩略图 + 媒体预览**(oprim **v3.22.0** `media_probe`+`video_thumbnail` PR #27/#28;aegis `fddeba5` Dockerfile 加 ffmpeg + pin bump + generate_thumbnail 视频分支 + `/files/media`;console `a09571f` 详情面板视频缩略图+元数据)。全量 1012 passed/0 fail。

## 工程完成度:CasaOS-parity 全部能力已实现并 live
存储观测+管理(mount/format/power)、文件分享、图片+视频缩略图、媒体元数据、DDNS(+cron)、Tailscale+ZeroTier VPN、宿主终端 —— 后端+前端+oprim(v3.22.0)全部提交。部署:重建 aegis-backend 镜像(含 ffmpeg + oprim v3.22.0)+ console 从工作区构建。

## 待主库定(每 PR body 标注)
- 元素命名(`block_device_list` 等)、风险默认值(mount 默认 dry_run)、smartctl/lsblk 未安装时的降级返回契约。
- 宿主变更类是否进 oprim 主库(通用)还是 aegis 专有层——倾向进 oprim 但默认 dry_run,执行策略留 aegis。

## 参考
- 盘点来源:CasaOS-{Core,AppManagement,AppStore,LocalStorage,UserService,Gateway,MessageBus,JobManagement}。
- 相关记忆:[[aegis-3o-extension-workflow]]、[[aegis-3o-elements-progress]]、[[aegis-appstore-install-flow]]、[[aegis-caddy-edge-routing]]。
