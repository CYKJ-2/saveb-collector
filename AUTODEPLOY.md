# 自动发布：GitHub 构建，内网 runner 部署

本地验证代码后 push main，GitHub 云端测试并构建 Linux amd64 镜像，将镜像推送 GHCR；192.168.11.84 上的 runner 主动领取发布任务，只拉镜像、备份、增量迁移、更新容器及验证健康。服务器不构建应用镜像，不需要云端 SSH 到内网，也不使用服务器 SSH 私钥。

## 已实现的流程

| 环节 | 实现 |
|---|---|
| 触发 | 各仓库 main push，或 Actions 手动运行；没有 PR 到生产 runner 的触发器 |
| API 测试 | 云端临时 PostgreSQL、单元测试、六组独立 schema 集成回归 |
| Admin 测试 | 根 Dockerfile 内执行 scripts/test-*.mjs 和 npm run build，再复制 dist 到 Nginx 镜像 |
| Collector 测试 | 单元及模拟接口测试；需要真实测试库的集成用例在未配置时跳过，首次上线仍须实际验收 |
| 镜像 | 根 Dockerfile，GHCR 标签 sha-完整提交号；部署使用不可变 sha256 digest |
| 发布 | automation/release.sh，共享 /home/admin_chen/www/.saveb-release.lock，三个项目不会同时迁移/切换 |
| 备份 | API/Collector 迁移前导出新业务库，检查归档、生成 SHA256；任何一步失败即停止 |
| 健康 | Compose 等待最多 300 秒；未配置健康检查的服务仅确认运行，还须业务验收 |
| 恢复 | 启动检查失败尝试恢复上次已记录健康的镜像和 Compose；仍将本次 Actions 标为失败 |
| 手动回滚 | Actions 输入已成功版本的完整 SHA；跳过构建和数据库迁移 |

源代码目录仍只需要根 .env、nginx.conf 等运行配置。automation/ 是发布源码，不属于运行镜像；没有重新引入 build/env 或另一套 Dockerfile。

## 当前仓库和操作顺序

| 项目 | GitHub 仓库 | GHCR 镜像 |
|---|---|---|
| API | [CYKJ-2/saveb-api](https://github.com/CYKJ-2/saveb-api) | `ghcr.io/cykj-2/saveb-api` |
| Admin | [CYKJ-2/saveb-admin](https://github.com/CYKJ-2/saveb-admin) | `ghcr.io/cykj-2/saveb-admin` |
| Collector | [CYKJ-2/saveb-collector](https://github.com/CYKJ-2/saveb-collector) | `ghcr.io/cykj-2/saveb-collector` |

发布脚本现在只接受 CYKJ-2 下对应应用的镜像 digest，旧 Ding-CYKJ 镜像不能用于这次新部署。Git 拉代码的 SSH 密钥与 Actions 的 GHCR 登录相互独立。

1. 三个仓库先设置仓库变量 `DEPLOY_ENABLED=false`，再提交并推送本次 `automation/`、`AUTODEPLOY.md`、`SERVER-DEPLOY.md` 修正（API 另有 CONFIGURATION.md）。检查暂存区，不要把其他尚未准备发布的业务修改顺带提交。
2. 确认 `.github/workflows/release.yml` 已在 main；GitHub Actions 的 test/build 成功，deploy 暂时显示 Skipped 是正常情况。
3. 服务器三个目录分别 `git pull --ff-only origin main`。按 SERVER-DEPLOY.md 填写 .env、准备基础设施和数据；拉过代码不等于完成此步骤。
4. 在新 CYKJ-2 仓库注册三个 runner，确认均为 Idle；旧仓库的注册信息不能继续服务新仓库。
5. 数据和配置核对通过后，按第 4 节逐个启用 API → Collector → Admin，首次通过 Run workflow 触发。之后 push main 自动发布。

服务器 www 目录重新建过，也不能据此认定数据库是空的：Docker 数据卷仍可能保留。首次启动 infra 前先执行只读 `docker volume ls --filter name=saveb-production`，核实是否存在本次新系统的旧卷及其密码、数据来源，不删除这些卷来绕过配置问题。

## 1. GitHub 权限和费用

各仓库 Settings → Actions → General 允许使用工作流所引用的 Actions，包括 actions/*、docker/* 和 API 的 shivammathur/setup-php。工作流声明最小权限：构建 job 为 contents:read、packages:write；发布 job 为 contents:read、packages:read。无需手动创建 GITHUB_TOKEN 或把它填入 Secrets。

工作流通过 OCI source 标签关联上表中的仓库；如果同名 package 已存在，应在 package 的 Settings → Manage Actions access 授予对应仓库写入权限，或确认它继承对应仓库权限。组织限制创建 package 时需由组织管理员开启相应权限。正常情况下服务器 runner 也使用本次 job 的 GITHUB_TOKEN 拉取本仓库镜像，不需要另配长期 GHCR PAT。[GHCR 身份认证和仓库关联](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

每个仓库打开 Settings → Secrets and variables → Actions → **Variables → New repository variable**，名称 `DEPLOY_ENABLED`，初始值 `false`。不需要创建名为 GITHUB_TOKEN 的 Secret，也不需要设置 SSH_HOST、SSH_PASSWORD 或 SSH_PRIVATE_KEY。仓库默认 Workflow permissions 可保持只读；工作流已在各 job 显式申请所需权限，如果组织策略禁止 Actions 或 package 写入，需要先调整对应策略。

截至 2026-09-10 的官方规则：

- GHCR 容器镜像存储和带宽当前免费，不是超过 10 GB 就收镜像费；政策改变至少提前一个月通知。[Packages 计费](https://docs.github.com/en/billing/concepts/product-billing/github-packages)
- 私有仓库标准云端 runner 消耗账户额度。Free/Free organization 每月 2,000 分钟，Pro/Team 每月 3,000 分钟，同账户各仓库共用。服务器 self-hosted runner 当前不收 Actions 执行费。[Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- 每仓库 Actions 缓存默认免费 10 GB，缓存额度与 GHCR 镜像不同。保留默认上限，不启用付费扩容；超出缓存上限时旧缓存被淘汰。当前工作流使用 mode=min 构建缓存，关闭 Docker build record artifact 上传，也不把 Docker 镜像打包为 Actions artifact。
- 同一账户其他任务也消耗额度，无法只凭三个项目保证总费用为零。组织/账户 Billing & Licensing → Budgets and alerts 中为 Actions 设置符合预期的预算；若要求只用免费额度，将付费预算设为 0（按账户可用界面设置），并确认勾选 Stop usage when budget limit is reached。仅开启邮件告警不会阻止继续计费。[预算设置](https://docs.github.com/en/billing/how-tos/set-up-budgets)

这次代码修改不会更改 GitHub 账户预算、缓存上限或付费设置。镜像大小和实际构建时长需首次云端运行后测量，不能用服务器扩容容量推算。旧镜像会逐渐积累，目前没有自动删除 GHCR 版本，避免误删回滚目标；后续清理须保留当前版本、上一健康版本及需要保留的回滚版本。旧业务数据库和附件不打包到镜像。

## 2. 一次性服务器准备

三个项目已 clone 到 /home/admin_chen/www，在本地提交并推送本次配置后，服务器各项目 `git pull --ff-only origin main` 一次获取最新配置。保留已有 .env；旧 .env 若是链接，先保存内容为普通文件再拉取。数据迁移和固定端口见 [SERVER-DEPLOY.md](SERVER-DEPLOY.md)。

服务器检查：`docker info`、`docker compose version`、`command -v bash flock python3`，以及 13000/18088/18085 端口占用。runner 用户需要能执行 Docker，沿用 admin_chen 现有权限。自动发布使用 Docker 正常的数据目录，但不得清理旧 ERP、禅道资源。

API 的独立 infra 必须先准备好：

```bash
cd /home/admin_chen/www/saveb-api
docker compose -f docker-compose.infra.yml config --quiet
docker compose -f docker-compose.infra.yml up -d --wait
```

这仅准备数据库/Redis/网络，不等于完成业务数据导入。首次上线必须先明确现有 RBAC 来源、准备兼容结构及迁移记录，迁入旧业务数据和真实附件。自动脚本只负责后续增量迁移，不初始化空业务库或重置管理员。迁移演练和正式切换写入窗口单独安排。

## 3. 在内网注册 runner

目前按仓库级 runner 配置，三个目录互相独立：

| 仓库 | runner 目录 | 名称 | 自定义标签 |
|---|---|---|---|
| saveb-api | /home/admin_chen/www/runners/saveb-api | saveb-api-84 | saveb-production |
| saveb-admin | /home/admin_chen/www/runners/saveb-admin | saveb-admin-84 | saveb-production |
| saveb-collector | /home/admin_chen/www/runners/saveb-collector | saveb-collector-84 | saveb-production |

每个仓库打开 Settings → Actions → Runners → New self-hosted runner，选择 Linux x64，使用页面当前下载地址和校验值。之前磁盘满导致解压不完整的目录应先核实是否已经注册/运行，补全同版本安装文件或准备独立新目录，不能盲目删除 .runner 等注册信息。不要重复启动同一注册实例。

未注册的目录执行页面提供的 ./config.sh 命令并加名称与标签，以 API 为例：

```bash
./config.sh --url https://github.com/CYKJ-2/saveb-api --name saveb-api-84 --labels saveb-production --work _work
```

按提示输入该仓库页面刚生成的注册 token（短期有效，不是 GHCR API Key）。Admin/Collector 改成对应仓库和名称。saveb-production 是调度标签，不是目录；_work 是 runner 临时源码工作目录。注册后 `./run.sh` 能显示 Listening for Jobs，GitHub 中应显示 Idle。[GitHub 注册说明](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)

三个新仓库的配置页面分别为 [API runners](https://github.com/CYKJ-2/saveb-api/settings/actions/runners)、[Admin runners](https://github.com/CYKJ-2/saveb-admin/settings/actions/runners)、[Collector runners](https://github.com/CYKJ-2/saveb-collector/settings/actions/runners)。下载、校验和解压命令在对应 runner 目录执行，不要在业务项目根目录执行，也不要再次 mkdir 嵌套的 actions-runner 目录。一个安装目录只注册一个实例；三个实例可以运行在同一台服务器。服务端无需公网入站端口，但必须能出站访问 GitHub Actions 服务、ghcr.io 及镜像下载地址，git clone 成功不能替代这些连接检查。

当前限制不修改 www 外的系统配置，所以不运行 sudo ./svc.sh install，不改 authorized_keys。确认没有另一实例运行后，可在每个 runner 目录用下面命令保持后台进程：

```bash
umask 077
nohup ./run.sh > runner.log 2>&1 < /dev/null &
```

该方式能在 SSH 退出后继续运行，但**服务器重启后不会自动启动 runner**，需要再次启动并确认 Idle。若需要开机自启，须另行确定允许的服务管理方式；本次不会偷偷创建 /etc/systemd/system 下的服务。runner 离线时 GitHub 发布任务排队，不代表已经部署。

## 4. 首次启用发布

无需建立 GitHub Environment。免费套餐的私有仓库不支持 Environments，因此当前工作流不绑定 environment，由 main 分支条件、仓库 DEPLOY_ENABLED 开关和服务器 .deploy-ready 控制发布。仓库 Settings → Secrets and variables → Actions → Variables 设置 `DEPLOY_ENABLED=false`；这是仓库变量，不是 .env 参数。现有 Environment 中的变量/审批规则不会作用于这套未绑定 Environment 的工作流，需要人工审批时另行配置。[GitHub Environment 套餐说明](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)

先 push，让云端测试和构建通过。部署 job 此时跳过，可先确认 GHCR package 及权限。工作流代码必须已经提交到 main；本地文件不能自行触发 GitHub。

完成数据库/附件准备、生产配置核对、确认自动采集可以开始后，在服务器三个项目内分别创建就绪标记：

```bash
touch /home/admin_chen/www/saveb-api/.deploy-ready
touch /home/admin_chen/www/saveb-collector/.deploy-ready
touch /home/admin_chen/www/saveb-admin/.deploy-ready
```

标记只表示人工确认首次数据准备完成；不要提前创建来绕过准备步骤。发布脚本还会检查新库 users 有数据，但它无法替代完整的 RBAC、订单和附件核对。

随后按 API → Collector → Admin 顺序，将对应仓库 DEPLOY_ENABLED 设为 true，在 Actions → Build and deploy production → Run workflow 选择 main，rollback_sha 留空。每个项目成功且业务验证通过后再启用下一个。初次没有历史健康版本可回滚，失败时保留 pending 并提示人工检查；不要直接删除状态文件强行继续。

## 5. 日常发布与回滚

日常只需本地测试、commit、push main。看 GitHub Actions 中 test/build/deploy 的状态；只有 deploy 成功并完成业务验证才表示发布完成。新 main 已出现时旧工作流会跳过部署，避免把已过时提交发布上去。跨仓库互斥锁保证发布串行，但不是三个仓库原子升级；接口和迁移应兼容上一版本。

每次成功记录在服务器项目 current、previous、releases.log，releases/完整SHA 保存 Compose、image.ref 和 succeeded。服务器原 clone 源码不自动 git pull，运行版本以 current 和镜像 digest 为准；.env/nginx.conf/config 仍由服务器维护。基础设施变更单独维护，不能用 app 发布替代数据库配置更新。

手动回滚：Actions → Run workflow → main → rollback_sha 填该项目曾成功发布的完整 40 位 SHA。脚本只接受服务器存在 succeeded 记录的版本，重新拉取对应 digest 并检查健康，不执行 migrate:rollback，也不恢复或覆盖业务数据。镜像被 GHCR 删除时回滚会失败，不能提前清理目标版本。相同 SHA 已记录的镜像 digest 不允许被另一次重建覆盖。只有部署失败、镜像已构建成功时，优先在原 Actions 运行中选择 Re-run failed jobs，复用原镜像输出；需要更改镜像时提交新版本。不要通过删除 releases 记录绕过一致性检查。

自动恢复只恢复应用镜像与 Compose，**不能撤销数据库结构变化，也不回滚 .env 或 nginx.conf**。迁移必须兼容旧应用；有破坏性迁移应先关闭 DEPLOY_ENABLED，采用经过验证的单独发布方案。现有人工启动的版本没有 releases/succeeded 记录，不会自动成为首次发布的回滚目标。

API/Collector 发布备份在 /home/admin_chen/www/backups/release-*；包含完整新业务库、归档目录和 SHA256。归档可读性检查不是恢复演练，也不包括附件目录；现有 Collector 备份恢复验证功能继续独立使用。脚本不自动清理备份、镜像或数据卷，定期按保留策略管理，禁止全局 docker system prune / volume prune / down -v。

## 本次验证范围

发布脚本使用隔离临时目录和模拟 Docker 命令做故障回归，不连接业务库；检查 Compose 中端口、资源名及 RELEASE_IMAGE 注入。云端 Actions 权限、实际 GHCR 镜像构建、服务器 runner 注册和完整上线验收仍需在真实环境完成。工作流和脚本准备完成不代表 GitHub/服务器已启用。

本地验证记录：发布故障回归 14 项通过；Admin 42 项测试及生产构建通过；API RBAC 16 项测试、107 个断言通过；Collector 32 项通过，46 项因未配置隔离集成库而跳过。验证时补回 API 已被引用但缺失的 PermissionNameSeeder，并修复 Admin 导航测试对新 external-menu 模块的加载；没有对业务库执行 Seeder。

后续 API 验证：六组集成回归均通过（Workbench 有 1 项跳过）；单元测试 24 项完成，有 1 项 PHPUnit warning。还修复了首次空库采集菜单迁移缺失 system 父节点的错误，并将本地初始化测试的旧权限数量断言改为实际菜单归属和路由权限覆盖检查。以上初始化与迁移验证仅在随机 rbac_test_* schema 执行，没有重建或初始化现有业务库。

CYKJ-2 迁移补充验证：发布回归增加三个应用的新账号镜像成功部署、拒绝旧账号/错误域名/错误应用/无效 digest，以及拒绝篡改后的回滚镜像；共 16 项用例。真实 GitHub 权限、云端构建和服务器上线结果以各自 Actions 日志为准。
