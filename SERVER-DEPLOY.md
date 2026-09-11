# 服务器配置与首次数据准备

当前采用 **本地 push → GitHub Actions 云端测试/构建 → GHCR → 内网 runner 拉镜像部署**。自动发布入口、runner 注册、GitHub 权限、首次启用及回滚见 [AUTODEPLOY.md](AUTODEPLOY.md)。服务器不执行应用镜像构建，日常发布也不用手动 git pull。

2026-09-10 已根据用户提供的 df/lvs 确认服务器扩容成功：根文件系统约 588 GB，可用约 507 GB。代码配置已准备不等于服务器已经部署成功；首次镜像构建、数据库迁移及访问入口仍需实际验证。

## 生产镜像与内网访问（2026-09-11 更新）

生产 Compose 只接受显式 RELEASE_IMAGE，不包含 build；先确保当前提交的 GitHub Actions build 成功，再在该项目目录 export RELEASE_IMAGE 为对应 ghcr.io/cykj-2/项目名:sha-完整提交号，docker pull 成功后启动。每次切换项目都重新设置 RELEASE_IMAGE，不能把 API 镜像用于 Collector 或 Admin。操作步骤见 API 仓库的 [应用启动说明](../saveb-api/APPLICATION-START.md)。

Admin 的 .env 设置 ADMIN_BIND_IP=192.168.11.84、ADMIN_PORT=13000，浏览器通过 http://192.168.11.84:13000/dashboard/overview 访问。API 的 APP_URL、FRONTEND_URL、CORS_ALLOWED_ORIGINS 使用同一入口。Admin Nginx 仍监听容器内 80，并代理 /api 到 saveb-api-web:8080；API/Collector 无需向浏览器开放宿主机端口。未设置 ADMIN_BIND_IP 时默认回环监听。

Collector 初次空业务库只有结构时，先运行 python scripts/migrate.py 并启动 api 服务即可。站点规则上下文、汇率和来源账号准备好后，python scripts/preflight.py 通过，再启动 worker/history/maintenance/logistics/beat。/ready 健康仅表示 Collector 表版本可用，不代表规则、来源登录或采集任务已经验收。

## 文件与配置

| 项目 | 构建文件 | 服务器 Compose | 需要维护的配置 |
|---|---|---|---|
| saveb-api | 根目录 Dockerfile，production 阶段 | docker-compose.server.yml | 根目录 .env、nginx.conf |
| saveb-admin | 根目录 Dockerfile | docker-compose.yml | 根目录 .env、nginx.conf |
| saveb-collector | 根目录 Dockerfile | docker-compose.server.yml | 根目录 .env；config/ 可为空 |

API 的 docker/ 包含 PHP 容器启动、上传限制和健康检查；自动发布工具独立放在 automation/。API、Collector 本地开发继续使用各自原有 docker-compose.yml；不要把本地 Compose 与 server Compose 叠加。

三个项目现在都只提供根目录 .env.example。仅在没有真实 .env 时复制模板，不覆盖现有 APP_KEY、账号密码或服务 Token。服务器核对 APP_ENV=production、APP_DEBUG=false、API_PORT=18088，生产 Collector 地址使用 http://saveb-collector-api:8080。API 与 Collector 共用新业务数据库和相同服务 Token。

保留独立项目名 saveb-api-production、saveb-admin-production、saveb-collector-production、saveb-infra，以及新网络 saveb-production，不操作旧 ERP/禅道的容器或卷。API、Admin、Collector 默认仅绑定 127.0.0.1 的 18088、13000、18085；后台内网/域名入口需要另外配置，修改 APP_URL 不会自动开放监听地址。

## 本次服务器端口与数据迁移约定

| 项目 | 服务器根目录 .env 设置 | 宿主机监听 | 容器内 HTTP 端口 |
|---|---|---|---|
| saveb-admin | `ADMIN_PORT=13000` | `127.0.0.1:13000` | `80` |
| saveb-api | `API_PORT=18088` | `127.0.0.1:18088` | `8080` |
| saveb-collector | `COLLECTOR_PORT=18085` | `127.0.0.1:18085` | `8080` |

已有服务器 `.env` 中的端口值会覆盖 Compose 默认值，更新源码后也要核对该文件，服务器使用上表值。容器间继续使用 `saveb-api-web:8080`、`saveb-collector-api:8080`，无需将 nginx.conf 的内部端口改成宿主机端口。上述回环地址仅服务器自身可访问；尚未配置域名/对外反向代理，不能直接在开发电脑用这些地址访问服务器。

### 三个 .env 的首次填写

模板已统一为 Linux 生产示例，使用 UTF-8（无 BOM）编码；本地 .env 与模板字段一致，但各机器的值独立维护。服务器通常只需填写 API 的 APP_KEY、数据库密码、Redis 密码、共享 Token，以及 Collector 的同库 DSN、相同 Token、收单账号和密码，再核对实际访问入口。其他字段按模板中文说明保留默认值。Admin 没有业务密码，仅配置端口和网络。

旧文件中没有的新字段按模板补齐；已有 .env 不要直接覆盖。APP_TIMEZONE 不是当前 API 的有效配置，Laravel 时区仍由 config/app.php 决定。普通采集间隔在采集管理页面设置，不要添加已停用的 SAVEB_COLLECT_INTERVAL_MINUTES。

在服务器执行，已有 .env 时保留原文件，不显示其内容：

```bash
for app in saveb-api saveb-admin saveb-collector; do
  root="/home/admin_chen/www/$app"
  if [ ! -e "$root/.env" ] && [ ! -L "$root/.env" ]; then
    (umask 077; cp "$root/.env.example" "$root/.env") || break
  fi
done
mkdir -p /home/admin_chen/www/saveb-collector/config
```

分别使用 `nano /home/admin_chen/www/项目名/.env` 编辑。API 需核对：

| 参数 | 本次服务器值 |
|---|---|
| APP_ENV / APP_DEBUG | `production` / `false` |
| API_PORT / DEPLOY_NETWORK | `18088` / `saveb-production` |
| APP_URL / FRONTEND_URL / CORS_ALLOWED_ORIGINS | 暂均用 `http://127.0.0.1:13000`，通过文末 SSH 隧道验收；以后改真实入口 |
| APP_KEY | 保留对应新系统原密钥，不能留空或随意重置 |
| DB_CONNECTION / DB_HOST / DB_PORT | `pgsql` / `saveb-api-postgres` / `5432` |
| DB_DATABASE / DB_USERNAME / DB_PASSWORD | `saveb` / `saveb` / 实际数据库密码；已有生产卷时以原配置为准 |
| REDIS_HOST / REDIS_PORT / REDIS_PASSWORD | `saveb-api-redis` / `6379` / 实际 Redis 密码 |
| CACHE_STORE / SESSION_DRIVER / QUEUE_CONNECTION | 均为 `redis` |
| SAVEB_COLLECTOR_URL / SAVEB_COLLECTOR_ACCOUNT | `http://saveb-collector-api:8080` / `default` |
| SAVEB_COLLECTOR_TOKEN | 至少 32 字符的随机服务 Token，与 Collector 一致 |
| BUSINESS_ATTACHMENTS_ROOT | `/data/attachments` |
| RBAC_SEED_ADMIN_PASSWORD | 保留现有 RBAC 时留空，不执行重置种子 |

Collector 需核对：

| 参数 | 本次服务器值 |
|---|---|
| COLLECTOR_PORT / DEPLOY_NETWORK | `18085` / `saveb-production` |
| SAVEB_DATABASE_URL | `postgresql://saveb:实际密码@saveb-api-postgres:5432/saveb`，数据库名/用户名/密码与 API 一致 |
| SAVEB_REDIS_URL | `redis://saveb-collector-redis:6379/0` |
| SAVEB_CELERY_BROKER_URL | `redis://saveb-collector-redis:6379/1` |
| SAVEB_CELERY_RESULT_BACKEND | `redis://saveb-collector-redis:6379/2` |
| SAVEB_API_TOKEN / SAVEB_SOURCE_ACCOUNT | 与 API 的 Token 一致 / `default` |
| SAVEB_PUBLISH_API | `true` |
| SAVEB_DH_BASE_URL | `https://www.dh-order.com` |
| SAVEB_DH_USERNAME / SAVEB_DH_PASSWORD | 在服务器私有 .env 填写收单账号和密码；保留原字符，不提交 Git |
| SAVEB_DH_COOKIE | 使用账号登录时留空 |
| SAVEB_RULES_FILE / SAVEB_RATES_FILE | 留空，从已迁入的 API 规则数据加载 |
| SAVEB_LOGISTICS_ENABLED | 密钥未配置时为 `false` |
| SAVEB_AFTERSHIP_API_KEY / SAVEB_KUAIDI100_API_KEY | 暂留空 |

DSN 中密码含 `@`、`:`、`#`、`%` 等特殊字符时，需要 URL 编码。只对全新基础设施生成新密码，已有卷的数据库密码不会随 .env 自动改变。普通订单自动采集间隔保存在数据库中，通过 Admin 系统管理下的采集管理设置；SAVEB_PENDING_INTERVAL_MINUTES 是独立的历史 Pending 发现间隔。

Admin 只需 `ADMIN_PORT=13000`、`DEPLOY_NETWORK=saveb-production`。不要给三个 .env 添加固定 RELEASE_IMAGE，镜像由工作流按 digest 注入。

当前无需改 nginx.conf：API 保留 `listen 8080`、`fastcgi_pass app:9000`；Admin 保留 `listen 80` 和 `http://saveb-api-web:8080`。它们均为容器内配置，不能把这些端口改成宿主机的 18088/13000。Collector 不需要 nginx.conf。

本次上线的数据库目标是：**保留新系统目标库现有 RBAC 数据，其余业务数据从旧线上系统迁移；Collector 随后写入同一个 API 业务库。** 这不是整库覆盖，也不是重新初始化权限。

2026-09-10 用户已确认：要保留的 RBAC 来源为**本地 saveb-api 数据库**。首次需把这批账号、角色、权限及关联授权带到服务器目标库，并核对对应 APP_KEY；不能把服务器空库新建的默认管理员当作保留 RBAC。其余业务数据仍以旧线上系统为来源。本次部署配置不自动导出、传输或导入这些数据，数据迁移验收前保持 DEPLOY_ENABLED=false，且不创建 .deploy-ready。

- 保留目标库 `users`、`roles`、`permissions`、`user_roles`、`role_permissions`、`api_tokens`、`audit_logs`；不以旧系统同名表覆盖账号、密码、菜单、角色和授权。
- 目标库 Laravel `migrations` 记录按目标结构核对并保留，不能用旧系统的迁移记录替换；`knex_migrations` 等旧框架元数据不当作业务数据直接套用。
- 迁移订单、人工调整、删除标记、采购、仓库、发票、PayPal、统计及附件等业务数据，实际执行前列出源表到目标表、字段和依赖的明确清单。旧库不存在的新系统表逐项确认保留或初始化，不能因源表缺失就清空目标表。
- 附件数据库记录和真实文件一起迁移；核对路径、数量及文件校验值。历史操作人 ID/UUID 与保留的用户不一定对应，先核对关联并制定映射或历史引用兼容方案，不自动改成管理员，不全局关闭外键。
- 先备份目标库及附件，再在独立临时库试导入。正式导入期间暂停新系统写入和 Collector；源库只读导出。导入前后对比 RBAC 内容、业务记录数/关键字段、序列和外键，通过后再启动采集。

此前本地导入经验见 saveb-api 的 `DATABASE-IMPORT-20260907.md`，其中表数量、外键处理及附件缺失情况只是当时的记录，不代表本次线上迁移结果。根目录历史 `copy-*.ps1` / `copy-*.sh` 不是本次生产迁移入口，部分脚本包含清表或级联逻辑，不能直接复用。当前配置变更没有执行任何线上数据迁移。

## 首次准备顺序

1. 本地验证并提交三个仓库到 main；部署开关 DEPLOY_ENABLED 暂为 false，让 GitHub 先完成云端测试与镜像构建。
2. 服务器三个现有仓库分别 `git pull --ff-only origin main` 获取一次配置和工具，保留真实 .env 和根目录 nginx.conf。遇到旧 .env 符号链接先保存内容为普通文件。
3. 服务器填写三个 .env（API 生产参数见 CONFIGURATION.md），Admin 设置 ADMIN_PORT=13000；Collector 创建 config/ 并填写来源账号、数据库 DSN 和服务 Token。API 与 Collector 共用同库，Redis 各自独立。
4. API 目录执行 `docker compose -f docker-compose.infra.yml config --quiet`，成功后 `docker compose -f docker-compose.infra.yml up -d --wait`，仅创建新生产基础设施。
5. 明确保留 RBAC 的来源、准备目标结构、迁入旧业务和附件并在临时库验证。空数据卷不能直接启动自动迁移代替首次导入。不要运行 migrate:fresh 或重置 RBAC。
6. 按 AUTODEPLOY.md 注册 runner，数据准备验收后创建各项目 .deploy-ready，逐个启用 DEPLOY_ENABLED，按 API → Collector → Admin 完成首次发布。
7. 在后台核对权限、历史业务、附件、采集间隔，再验证当天手动采集和自动任务。

## 配置保持与后续维护

.gitignore 排除真实 .env、配置备份、业务附件和发布状态。automation/、根 Dockerfile 和 Compose 必须提交。发布使用 runner 工作区的精确提交，并将 Compose 与镜像 digest 保存到服务器项目的 releases/；不会覆盖服务器 .env、nginx.conf、config/ 或改变业务卷名。

环境变量 RELEASE_IMAGE 由发布程序注入，普通 server Compose 保留本地镜像名作为手工维护默认值。不要在服务器 .env 固定 RELEASE_IMAGE 或手工启动旧镜像覆盖已发布版本。改 .env/nginx.conf 后通过 Actions 的发布流程重新创建应用容器。基础设施配置变更需独立审查和维护，自动发布不会重建数据库/Redis。

## 手工数据库备份参考

手工维护数据库前，先完成数据库备份。若使用上面的新基础设施，可在 API 目录执行（不适用于旧 ERP 数据库）：

```bash
mkdir -p /home/admin_chen/www/backups
umask 077
saveb_backup="/home/admin_chen/www/backups/api-$(date +%Y%m%dT%H%M%S)-$$.dump"
docker compose -f docker-compose.infra.yml exec -T postgres sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$saveb_backup"
```

确认上一条成功且文件非空，再检查归档可读：

```bash
test -s "$saveb_backup" && docker compose -f docker-compose.infra.yml exec -T postgres \
  pg_restore --list < "$saveb_backup" > /dev/null
```

备份失败或归档检查失败时停止后续修改。pg_restore --list 只是归档可读检查，不能替代临时库真实恢复验证；附件和异机备份单独维护。自动发布中的 automation/backup.sh 同样先备份及检查归档，再执行增量迁移。

## 访问和验收

服务器上执行：

```bash
curl -f http://127.0.0.1:18088/up
curl -f http://127.0.0.1:18085/ready
curl -f http://127.0.0.1:13000/healthz
```

暂无域名时在 Windows PowerShell 使用现有 SSH 认证建立验收隧道：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 13000:127.0.0.1:13000 admin_chen@192.168.11.84
```

核对主机指纹并按提示认证，保持窗口打开，浏览器访问 http://127.0.0.1:13000/dashboard/overview 。测试阶段 API 的 APP_URL、FRONTEND_URL、CORS_ALLOWED_ORIGINS 可使用 http://127.0.0.1:13000，正式入口启用后改为实际 URL。隧道不需要修改 authorized_keys；直接访问服务器内网 IP 的 13000 端口在当前回环绑定下不可用。

正式多人访问的反向代理/域名另行配置，不修改宿主机现有站点。所有本项目服务器文件放在 /home/admin_chen/www，Docker 按正常机制写 /var/lib/docker；不操作旧 ERP、禅道的容器、数据卷或全局清理命令。
