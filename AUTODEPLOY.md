# GitHub Actions + GHCR + Docker Compose 自动发布

## 日常只需推送代码
 
## 已关联的仓库与镜像

| 项目 | GitHub 仓库 | GHCR 镜像 |
|---|---|---|
| saveb-api | `git@github.com:Ding-CYKJ/saveb-api.git` | `ghcr.io/ding-cykj/saveb-api` |
| saveb-admin | `git@github.com:Ding-CYKJ/saveb-admin.git` | `ghcr.io/ding-cykj/saveb-admin` |
| saveb-collector | `git@github.com:Ding-CYKJ/saveb-collector.git` | `ghcr.io/ding-cykj/saveb-collector` |

工作流根据实际仓库名自动生成小写镜像名称，无需手填组织名。三个仓库分别配置发布 Secrets；`DEPLOY_ROOT` 分别为 `/home/admin_chen/www/saveb-api`、`/home/admin_chen/www/saveb-admin`、`/home/admin_chen/www/saveb-collector`。

本地把通过测试的代码推送到 main，GitHub Actions 自动构建 Linux amd64 镜像并推送 GHCR，再通过 SSH 让服务器切换版本。dev 不发布生产。部署使用镜像摘要，不使用会漂移的 latest；服务器不再需要 git pull、Node、Composer 或 Python 构建环境。

三个仓库分别发布，只更新自身服务。跨项目接口或数据库变更必须保持前后版本兼容；这不是三个仓库同时切换的事务。首次按 API → Collector → Admin 顺序部署。

已提供：
- `.github/workflows/release.yml`：main 自动构建、部署；Actions 手动回滚入口。
- `deploy/Dockerfile`：生产镜像；不会复制本地 .env、依赖缓存或业务数据。
- `deploy/compose.yml`：生产应用服务，仅使用 GHCR 镜像。
- `deploy/release.sh`：发布锁、迁移前备份检查、健康检查、失败回滚、版本记录。
- `deploy/test_release.py`：模拟 Docker 的发布/回滚故障测试。
- `deploy/runtime.env.example`：服务器私有配置模板。
- API 额外提供 `deploy/infra.yml` 和 `deploy/pre-migrate.example.sh`。

旧 `start-prod.sh`、`deploy-pull.sh` 和 `build/docker/*` 不是本方案的入口，不要与新生产 Compose 混用。本地现有 Docker 开发环境继续使用原命令。

## 服务器一次性准备

服务器目录已按约定设为：

```text
/home/admin_chen/www/
├── infra/                    单独管理 PostgreSQL、两个 Redis
├── backups/                  迁移前数据库备份
├── saveb-api/
│   ├── .env                  真实配置，不进 Git
│   ├── pre-migrate.sh        服务器自行管理的备份入口
│   ├── current               最近成功发布的完整提交号
│   ├── previous              上一个成功版本
│   ├── releases/提交号/       Compose、发布脚本和镜像摘要
│   └── releases.log          发布结果记录
├── saveb-admin/               同样保存 .env 和 releases
└── saveb-collector/           另需 config/，以及 pre-migrate.sh
```

1. 安装 Docker Engine 和 Docker Compose v2（需支持 `up --wait --wait-timeout`）、Bash、SSH、tar、flock、coreutils。部署用户为 admin_chen，需要访问 Docker 和上述目录。Docker 权限等同管理服务器，SSH 密钥应专用于这些受信任仓库。
2. 创建目录，复制三个项目各自的 `deploy/runtime.env.example` 为对应服务器目录的 `.env`，填写真实值，并设为仅部署用户可读写（chmod 600）。不要覆盖已经填写的配置。
3. API 的 APP_KEY 必须生成并长期保留，可使用已生成的 Laravel 密钥。数据库密码、Redis 密码、DH 账号密码以及两边一致的 Collector 令牌都放在服务器 .env。Collector 数据库 URL 中的密码须 URL 编码。
4. API 的 DATABASE 与 Collector 连接必须指向同一个库。导入已有业务数据和附件后，先确认数据正常，再启动自动采集。新网络固定默认 `saveb-production`，模板配置已对应内部服务别名。
5. Collector 的 `config/` 可保持为空，以数据库规则为准；使用覆盖配置文件时将路径设为容器内 `/app/config/...`。

创建目录示例（Linux）：

```bash
mkdir -p /home/admin_chen/www/{infra,backups,saveb-api,saveb-admin,saveb-collector/config}
```

将 API 的 `deploy/infra.yml` 复制到 `/home/admin_chen/www/infra/compose.yml`，使用已填写好的 API 环境配置启动基础设施：

```bash
docker compose -p saveb-infra \
  --env-file /home/admin_chen/www/saveb-api/.env \
  -f /home/admin_chen/www/infra/compose.yml up -d --wait
```

基础设施单独持久化，应用发布不会停止或删除它。若复用已有 PostgreSQL/Redis，需改网络与连接地址，并改备份入口；不要把新命名卷当作旧数据库。

将 API 的 `deploy/pre-migrate.example.sh` 分别复制为 API 和 Collector 服务器目录里的 `pre-migrate.sh`，chmod 700。此示例适配上述 saveb-infra，用 pg_dump 创建快照、检查归档可读性并生成 SHA-256。它不是完整恢复演练；原有定期备份恢复验证仍应单独保留。备份失败会停止发布。

## GHCR 和 SSH 权限（只配置一次）

GitHub 构建镜像使用自动提供的 GITHUB_TOKEN，不需把个人令牌写入工作流。

服务器以部署用户身份登录 GHCR；私有镜像需要能读取三个镜像包的令牌（read:packages），只输入到服务器 Docker 凭据配置，不写进仓库：

```bash
docker login ghcr.io -u 你的GitHub用户名
```

若组织启用了 SSO，还需为令牌授权该组织。不要清理仍需回滚的镜像包。

给 GitHub Actions 配置专用 SSH 密钥：公钥加入服务器 admin_chen 的 authorized_keys；私钥存入 GitHub Secret。SSH 主机公钥应通过服务器控制台或已有可信连接核对指纹，生成 known_hosts 内容。工作流严格校验主机公钥，不会自动信任扫描到的新主机。

## 每个 GitHub 仓库的设置

进入 Settings → Secrets and variables → Actions。也可以将 SSH Secrets 放入名为 production 的 Environment。

| 类型 | 名称 | 内容 |
|---|---|---|
| Secret | DEPLOY_HOST | Linux 服务器 IP 或 SSH 域名 |
| Secret | DEPLOY_USER | admin_chen |
| Secret | DEPLOY_PORT | SSH 端口，通常 22 |
| Secret | DEPLOY_SSH_KEY | 专用 SSH 私钥全文 |
| Secret | DEPLOY_KNOWN_HOSTS | 核对过指纹的 known_hosts 内容；非 22 端口需使用对应的端口格式 |
| 仓库 Variable | DEPLOY_ROOT | 对应项目的 /home/admin_chen/www/saveb-api 等目录；不填时按项目名称使用此默认值 |
| 仓库 Variable | DEPLOY_ENABLED | 完成服务器准备后设为 true；未开启时只构建推送镜像 |

**DEPLOY_ENABLED 必须是仓库级 Variable**，工作流在进入 production Environment 前就会判断它。

production Environment 若设置人工审批，每次部署都会等待审批；要实现 push 后全自动发布，就不设置 required reviewers，使用 main 分支保护来控制合入权限。仓库必须允许 Actions 使用 GHCR，镜像默认命名为 `ghcr.io/仓库所有者/仓库名`（转小写）。

推送 main 后，Actions → Production release 中查看构建和部署结果。首次配置尚未完成时 deploy 会跳过，这不等于服务器已经部署。

## 域名与访问

默认只监听服务器回环地址：
- Admin：127.0.0.1:18000
- API：127.0.0.1:18080
- Collector：127.0.0.1:18085

服务器已有 Nginx/宝塔/其他反向代理可以将管理域名 HTTPS 转发到 127.0.0.1:18000。Admin 镜像已将 /api/ 转发到内部 API，不需要公开 Collector。API 的 APP_URL、FRONTEND_URL、CORS_ALLOWED_ORIGINS 应与实际域名对应。域名和 TLS 证书尚需在服务器配置；不建议直接把回环端口改为公网开放来替代 HTTPS。

数据库、附件与运行状态保存在 Docker 命名卷中。首次服务器数据导入、附件复制和管理员账号初始化按业务迁移文档单独完成，流水线不会重置数据库或重新生成 APP_KEY。

## 自动回滚和手动回滚

健康检查失败后，脚本恢复最近成功版本的镜像及其 Compose，并再次检查；恢复成功也会让本次 Actions 标记失败，便于发现问题。首次发布没有成功旧版本可恢复，会保留 pending 标记并报告失败。

手动回滚：
1. Actions → Production release → Run workflow，分支选择 main。
2. 在 rollback_sha 输入服务器曾成功部署的完整 40 位提交号。
3. 运行工作流；回滚复用服务器保留的版本包，不重新构建，不执行数据库降级迁移。

服务器也可直接执行：

```bash
# 将 COMMIT_SHA 替换为真实的完整提交号
bash /home/admin_chen/www/saveb-api/releases/COMMIT_SHA/release.sh \
  /home/admin_chen/www/saveb-api COMMIT_SHA rollback
```

回滚范围是应用代码和 Compose。数据库数据、已发布任务、附件及服务器 .env 不会回滚。数据库迁移必须遵循向后兼容原则：先加字段/表并兼容旧代码，再在后续版本清理。删除字段、改字段含义等破坏性迁移可能让旧镜像无法启动；脚本会报告回滚失败，不能保证自动修复这种数据结构变化。

应用切换过程中可能有短暂中断，这套方案不承诺零停机。对三个仓库配套变更应先发布兼容的新 API，再发布 Collector/Admin。

服务器 .env 由运维维护，旧镜像回滚仍读取当前 .env。变更配置前需另行留存配置备份，不能把“代码回滚”理解成完整环境快照恢复。

## 失败与恢复

- 构建失败：不会进入服务器部署。
- SSH/GHCR 拉取失败：查看认证或网络，原运行版本通常不受影响。
- 备份/迁移失败：不切换应用，保留失败记录；检查迁移是否已经部分生效，再修复重发。
- SSH 中断留下 pending：若有 current，下次操作先恢复该版本；如果首次发布没有 current，先人工检查服务状态，再清理该项目的 pending 标记后重试。
- 同一提交号的部署包不可覆盖；若同一 SHA 重建后摘要不同会拒绝覆盖，修正后用新提交发布。
- 回滚版本不得是仅构建过、未健康部署过的版本。
- 发布锁保证同一个 www 父目录下的项目串行切换；它不能代替跨项目兼容性设计。

保留 releases、current、previous 和 GHCR 历史镜像。不要在发布脚本里执行 docker compose down -v、数据库重置或自动清空旧镜像。

## 验证边界

本地已使用模拟 Docker 测试发布状态和失败恢复；生产环境变量与 Compose 可以独立做 config 校验。首次 GitHub Actions 构建、私有 GHCR 拉取、真实 SSH、域名/TLS 和服务器健康检查，必须在上述一次性接入完成后验证，不能把本地测试当作线上部署已经成功。

官方参考：[GitHub 工作流触发](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)、[Docker Compose up 与等待健康检查](https://docs.docker.com/reference/cli/docker/compose/up/)。
