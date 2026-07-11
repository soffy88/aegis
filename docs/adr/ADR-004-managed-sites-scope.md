# ADR-004: 建站/托管纳入受管资产（§0.1 定位修订）

**日期**: 2026-07-10
**状态**: Accepted
**决策者**: Aegis 项目 owner

## 背景

`§0.1 定位` 原文将 Aegis 的差异化限定为"把观测与动作闭合成一个可审计、有护栏的环"，并立下：

> 闭环是价值单元。任何不加强某一环、或加宽却不闭合的功能，是 scope creep，不予接线。

在此约束下，现有的"一键建站"（`api/routers/websites.py`）处于灰色地带：它 `subprocess docker run` 起一个 nginx/php 容器就返回，**不入库、不接监控、不接自愈、不写审计**——是一段"加宽 Execute 环但不闭合"的功能，按 I1/I2 应计为负债。

owner 的实际诉求（2026-07-10）：想用 Aegis 自托管起新站点，但

1. 站点类型太少（只有 static / PHP），想用 Next.js + 自研 OUI 组件库等现代栈搭新站**没有入口**；
2. 更重要的是这功能太浅——建完就撒手，不像个受管的东西。

诉求 (2) 恰好指出了 (1) 能被接线的**前提**：只要把建站接入 MAPE-K（监控/自愈/审计），它就不再是 scope creep，而是一类受管资产。

## 决议：admit "自托管建站/托管"为受管能力，条件是闭环

修订 `§0.1`，在闭环红线之后增加一条 admit 子句（**不删除**红线）：

> **自托管站点/应用的部署与托管**，admit 为 Aegis 的受管能力域，**当且仅当**部署产物接入 MAPE-K：入库追踪（Knowledge）、健康探活回灌监控（Monitor）、异常经告警管道可达自愈（Execute）、生命周期操作留审计。**未闭环的建站（build-and-forget）仍是 scope creep / 负债，MUST NOT 计为资产。**

即：闭环红线不变，反而成为建站域的**准入执法条件**。"能动（容器管理可替代）"这句判断依然成立——裸起容器确实可替代；Aegis 的差异化在于这里的站点是**受管**的。

## 成熟度与不变量对齐

- **I1 闭环治理**：站点归属 Execute 环，但其价值兑现依赖 Monitor（探活）+ Knowledge（入库）闭合。投资顺序 MUST 先闭环（B）再加宽（A）。
- **I2 成熟度定资产**：站点能力按 L0–L4 标注。当前实现落地后目标 **L2**（入库 + 探活 + 审计 + oprim 原语；被动 `restart_policy` 兜底）。**深度自愈**（探活异常→告警→自愈插件重拉验证）为 L3，列为有界 follow-up，达成前 MUST NOT 宣称"自愈已闭环"。
- **I7 暴露层级**：站点对外经 Caddy :443；Aegis 控制面本身的暴露约束不变。站点是被 Aegis 托管的**被管对象**，不改变控制面自身的准入。
- **I9 自举分离**：站点是普通被管负载，可纳入托管；与"Aegis 本体不自管"不冲突。

## 落地边界（本 ADR 授权的实现范围）

1. **B（先，核心）**：`sites` 表入库；create/delete/list 走 `obase.docker` 原语（`docker_container_create/start/stop`）替代 raw subprocess；建站/删站写 `audit_log`；探活循环记录健康状态；容器打 `aegis.managed=true` 标签纳入受管视图。
2. **A（后，广度）**：`runtime` 预设（static / php / nextjs-oui）；"从模板新建站点"脚手架（`/websites/scaffold`，含 Next.js + OUI starter）落到文件管理器目录再部署。
   - **模板级一次性构建 admit**：nextjs-oui 脚手架配 `next.config output:'export'`；部署时在**临时 node 容器**里跑一次 `npm install && next build` 产出 `out/`，随后由 nginx 以**静态**方式托管——无常驻 node 进程、不运行时重建，对齐 §0.2 铁律。这与下条"任意 Git 源码构建"不同：构建输入是**平台自带的固定模板**，非用户任意仓库，攻击面与维护面都有界。
   - **OUI 私有 tarball vendoring**：OUI（`@helios/blocks`/`@helios/oui`）不发布公共 npm；脚手架从 `AEGIS_OUI_VENDOR_DIR`（默认 console 用的 `platform/OUI/`）拷 tgz 进站点 `vendor/`，`package.json` 用 `file:` 引用。**决议不发布公共 npm**——不可逆的公开暴露，且违背自托管/可离线定位（I7）。
3. **暂不做**：从**任意 Git 源码**自动构建（Coolify 式 buildpack，检测 Dockerfile/nixpacks 构建任意仓库）——攻击面/供应链无界，属新基建，另行 ADR 评估。

## 影响

- `DESIGN.md §0.1` 增补 admit 子句（本 ADR 同批修改）。
- `websites.py` 由野路子升级为受管路径；新增一张迁移表。
- 不改 3O 主库；新增对 `obase.docker` 的调用沿用 apps.py 既有形态（窄腰，见 ADR-003）。
