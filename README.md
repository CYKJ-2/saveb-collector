# saveb-collector

服务器构建支持旧版 Docker builder：Dockerfile 使用多阶段 COPY，不依赖 `RUN --mount` 或 buildx。`.wheels` 仅作为构建阶段的可选离线依赖来源，运行镜像只保留安装后的包。

当前服务器部署采用 **本地 push → 服务器 git pull → 服务器构建镜像并启动 Docker Compose**，无需 GHCR 或 runner。完整命令见 [服务器启动说明](../saveb-api/APPLICATION-START.md)；自动发布文档留作后续启用时参考。

服务器自动发布见 [AUTODEPLOY.md](AUTODEPLOY.md)：本地 push main → GitHub 云端测试/构建 → GHCR → 内网 runner 拉镜像部署和健康检查。首次数据迁移及固定端口见 [SERVER-DEPLOY.md](SERVER-DEPLOY.md)。真实 .env、nginx.conf 和业务数据由服务器独立维护。

DH-Order 收单数据采集服务，采用 **Python 3.12 + Litestar + Celery + Redis + PostgreSQL/asyncpg**。通过账号密码自动登录，直接写入 **saveb-api 对应的数据库**，在 saveb-admin 首页监控并手动触发。

完整部署和维护说明见 **[项目说明文档](项目说明文档.md)**；跨项目接口与权限见 [API 接入说明](../saveb-api/COLLECTOR-INTEGRATION.md)。

## 核心功能

采购物流查询已接入 saveb-admin 采购工作台，支持 AfterShip／快递 100、手动刷新和独立定时查询。配置、队列、写入字段及旧服务器密钥查找见 [物流采集说明](物流采集说明.md)。物流 API Key 仅保存在 Collector。

订单创建日期现固定使用来源 `createTime`，付款/完成/来源更新时间分字段保存。服务器旧数据可省略新增字段；升级及导入后的修正步骤见 [订单日期与旧数据迁移说明](订单日期与旧数据迁移说明.md)。

| 功能 | 当前行为 |
| --- | --- |
| 自动刷新 | 默认每 30 分钟，可在采集管理页设为 5–1440 分钟；刷新当天＋前一天、复查未完成订单并推进一个历史日分片 |
| 手动采集当天 | 首页按钮提交任务，只发布北京时间当天数据，与首页选中的统计日期无关 |
| 历史更新 | 指定范围重采、仅补缺、失败分片续跑、归档重算预览 |
| 自动登录 | 本地识别图片验证码，Redis 共享会话，失效后重新登录；兼容手工 Cookie |
| 首页监控 | `#refreshText` 展示采集结果、最近成功及调度健康 |
| 数据保护 | 分片事务、幂等、同账户锁、来源版本校验、保留人工调整与软删除 |
| 历史 Pending 发现 | 独立定时扫描，每 30 分钟一个 7 天历史区间；超分页预算自动拆分，失败不推进游标 |
| 采集对账 | 发布分片自动对账，每天北京时间 03:10 全范围对账；差异报告不修改业务数据 |
| 备份恢复验证 | 全库一致性快照、附件备份、SHA-256 校验、临时库真实恢复及逐表行数核对 |

运行时不依赖 saveb-erp，也不经过 saveb-sales-data 文件中转。原始响应保存在 `collector.chunks.raw`，业务数据直接更新同库 API 表。

新增三项的配置、手动命令、结果查询和故障处理见 [采集巡检与备份说明](采集巡检与备份说明.md)。升级已有环境需先执行迁移，再重启 API、worker、history、maintenance、Beat；备份使用独立操作系统定时任务，需要运行安装脚本。

## 本地启动

前提：saveb-api 数据库和 Docker 网络已启动，`.env` 中的数据库、来源账号、服务令牌已经配置。首次使用请按[项目说明文档](项目说明文档.md)的部署步骤完成配置，不能使用模板占位值直接启动。

在本项目目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1
```

脚本依次构建镜像、迁移 collector 表、检查数据库/Redis/规则、准备登录会话，再启动全部服务。只运行一个 Beat。

本机代理或 DNS 导致构建阶段无法下载依赖时：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1 -OfflineBuild
```

此选项先通过显式 DNS 下载 Linux 依赖到 `.wheels/`，再离线安装构建；首次准备缓存仍需要网络。

## 常用命令

在项目目录执行，任务由 worker 异步处理：

```powershell
# 采集北京时间当天
docker compose exec api python scripts/collect_today.py --wait
# 单次字段刷新
docker compose exec api python scripts/refresh_fields.py --wait
# 更新已有历史，起止日期均包含
docker compose exec api python scripts/backfill_history.py --start 2026-08-01 --end 2026-08-31 --mode refresh --wait
# 只补未成功覆盖的日期
docker compose exec api python scripts/backfill_history.py --start 2026-08-01 --end 2026-08-31 --mode missing --wait
# 预览，不发布业务订单
docker compose exec api python scripts/backfill_history.py --start 2026-08-01 --end 2026-08-31 --dry-run --wait
# 将 JOB_ID 替换为真实 UUID，仅重试失败分片
docker compose exec api python scripts/backfill_history.py --resume JOB_ID --wait
# 检查服务和日志
docker compose ps
docker compose logs --tail 100 api worker history maintenance beat
```

不带 `--wait` 的成功退出只表示提交成功。带 `--wait` 时成功返回 0，终态失败返回 1，等待超时返回 2；超时不取消后台任务。

## 配置要点

模板见 [.env.example](.env.example)，默认提供 Linux 服务器值，逐组标明必填项、本地差异和中文说明。本地 `.env` 与模板字段一致，各自维护不同值；真实密码、Cookie 和令牌仅保存在被 Git 忽略的 `.env`，不要提交。文件使用 UTF-8（无 BOM）；编辑器编码规则见 `.editorconfig`。

已移除无效的 `SAVEB_COLLECT_INTERVAL_MINUTES`、`SAVEB_DH_LOGIN_URL` 和 `SAVEB_LOG_LEVEL`：普通采集间隔在采集管理页面设置，登录地址由收单适配器确定，Celery 日志级别由启动命令的 `--loglevel` 设置。`SAVEB_PENDING_INTERVAL_MINUTES` 只控制独立的 Pending 发现任务。

整理前的本机私有配置保存在 `.env.backup-时间戳`，该文件不提交 Git、不进入镜像。原有账号、密码、Token、连接地址保持不变，新增字段沿用原有运行默认值；生产模板中物流默认关闭，配置供应商密钥后再开启。

- `SAVEB_DATABASE_URL` 必须指向 API 实际数据库，不能仅凭数据库名称判断其是否为测试库。
- `SAVEB_PUBLISH_API=true` 才发布业务表；false 是影子采集。
- `SAVEB_API_TOKEN` 与 API 的 `SAVEB_COLLECTOR_TOKEN` 一致，至少 32 个字符。
- `SAVEB_SOURCE_ACCOUNT` 与 API 的 `SAVEB_COLLECTOR_ACCOUNT` 一致。
- 优先配置 `SAVEB_DH_USERNAME`、`SAVEB_DH_PASSWORD`；全角 `！` 与半角 `!` 不等价。
- Redis 使用专属主机名 `saveb-collector-redis`，避免与 API Redis 混淆。

修改 `.env` 后需重新创建容器；普通 restart 不会重新注入环境变量：

```powershell
docker compose up -d --force-recreate --no-build api worker history maintenance beat
```

代码变更需先重新构建镜像，再更新所有应用容器。API 配置变更也需让 Laravel 重新加载配置。

## 文档与代码入口

- [项目说明文档](项目说明文档.md)：项目关系、采集字段、入库机制、部署、脚本、接口、监控、排障和测试。
- [API 接入说明](../saveb-api/COLLECTOR-INTEGRATION.md)：接口、权限和首页联调。
- [来源协议](app/collectors/dh_order.py)、[登录会话](app/collectors/session.py)、[订单规范化](app/domain/orders.py)。
- [任务规划](app/services/jobs.py)、[采集执行](app/services/collection.py)、[订单合并](app/persistence/orders.py)。
- [Docker 服务](docker-compose.yml)、[迁移](migrations/001_collector.sql)、[测试](tests)。

`SOURCE_VERSION_CONFLICT` 的备注字段兼容处理和失败续跑方法见说明文档“故障排查”。

## 采集管理与定时代码位置

页面入口：**系统管理 → 采集管理**（/system/collector），支持保存采集间隔、自定义日期范围更新、仅补缺、归档重算预览及任务详情。菜单和各操作需分别授权。

定时检查在 [app/queue/celery_app.py](app/queue/celery_app.py) 的 check-collection-schedule，每 60 秒调用 [app/queue/tasks.py](app/queue/tasks.py) 的 check_schedule。实际采集间隔保存在 collector.schedules，默认 30 分钟；保存后无需重启。完整流程、接口和升级步骤见 [说明文档第 11 节](项目说明文档.md)。
