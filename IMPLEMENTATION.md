> 2026-09-07 接入修订：当前实现已改为 saveb-api 数据库 + saveb-admin 首页；以下 ERP 接入内容属于前一阶段记录。以 [最新接入说明](../saveb-api/COLLECTOR-INTEGRATION.md) 和 README 为准。

# 实现与验证记录

日期：2026-09-07。

实现范围：saveb-collector 采集、任务管理、持久化、调度、历史脚本，以及 saveb-erp 的受权限保护的采集入口。没有变更 saveb-deploy 或 saveb-sales-data 的业务文件，没有执行真实上游采集或生产切换。

## 验证结果

| 检查 | 结果 |
|---|---|
| Collector 单元、协议、数据库、故障及真实队列集成测试 | 31 项通过（完整测试命令见下方） |
| Ruff 格式与检查 | 通过 |
| ERP API TypeScript 构建 | 通过 |
| ERP web TypeScript 构建 | 通过 |
| ERP collector / route-registration / frontend-lifecycle 测试 | 11 项通过 |
| ERP typescript-runtime 扩展检查 | 4 项中 3 通过，1 项未通过：未修改的 legacyDashboard 导出集合比旧测试契约多 4 个已有函数 |
| 浏览器交互 | 本地 mock 服务中验证“立即采集”、历史日期提交、完成状态、分片计数及页面布局 |
| Docker Compose 配置 | 通过 docker compose config --quiet |
| Docker 镜像构建 | 未完成：Docker Hub auth.docker.io DNS 解析失败，无法拉取 python:3.12-slim-bookworm |
| 真实生产 DH-Order / 线上 DB | 未执行 |

Python 测试环境为本地 venv 的 Python 3.14，真实测试数据库为独立 PostgreSQL 16 容器，真实队列为独立 Redis 7 容器。ERP 构建使用当前 Node 24；仓库要求的 Node 20 尚未进行本次构建验证。生产 Dockerfile 固定 Python 3.12，依赖锁针对 Python 3.12 生成。

隔离数据库应用了 ERP 结构迁移。最后一个 202608120003_unmatched_site_grouping 是依赖特定历史业务数据的数据校正迁移，在空/测试数据集上触发 SCOPED_TOTALS_MISMATCH，因此没有强行绕过其校验；此测试不代表生产数据校正已完成。

ERP 旧契约失败详情：typescript-runtime.test.js 期望的 legacyDashboard 导出集合未包含 applyOrderOverridesToPayload、loadOrderUserOverrides、orderSearchIdentityValues、reconcileCanonicalSearchRows。本次没有改动该服务或放宽其测试断言。

## 实际测试覆盖

- 上游 POST 参数、完整 Cookie、code=1、分页、缺页、重复 ID、非 JSON/认证失败。
- 默认错误 dhgate 地址拒绝，生产禁止使用测试 HTTP 地址，错误中不泄漏 Cookie。
- 新增、更新、重复投递、Pending 转 Completed、Refunded 移除销售统计、跨日更新。
- 客服分摊金额守恒、测试订单排除、商品图片计数、未知汇率拒绝、旧汇率保持。
- 人工修改字段保留、旧更新时间拒绝覆盖、同版本不同源数据冲突回滚。
- 幂等任务、Redis 故障 outbox 保留、成功派发后看门狗保留、Worker 成功后清理。
- 影子覆盖不能误跳过正式 ERP 发布，dry-run 不写正式订单/水位，原始证据可重算预览。
- 同来源锁、取消、失败恢复、只重试失败分片、统计异常时事务回滚。
- 真实 Redis → Celery Worker → 本地模拟 HTTP → 实际 PostgreSQL → 任务完成。
- ERP 角色授权、CSRF、可信 actor 转发、上游异常不泄漏秘密。

## 与设计基线的具体落地方式

- 原始批次与规范化证据放在 collector.chunks，未再拆一个 raw_batches 表；它们仍按任务/分片可定位。
- 互斥使用同一 PostgreSQL 连接持有的会话 advisory lock，从请求一直覆盖提交。连接断开后旧 Worker 无法用该连接继续提交，因此不依赖 Redis TTL 或独立 fencing token。
- 数据访问使用 asyncpg 直接参数化 SQL；移除未使用 ORM/Django Beat 依赖，减少双套模型与事件循环池问题。
- 实时/历史处理都提交到同一 JobService。规则上下文、发布模式在受理时冻结；恢复保留原上下文。修正规则/汇率后应创建新任务，不把旧任务恢复当成换规则。
- reprocess 强制预览，避免旧归档覆盖当前源事实；真正历史字段更新使用 history 模式重新从上游读取。
- 兼容快照直接写入 ERP 数据库；文件导出是独立可选工具，不再从 completed_orders_*.json 反向驱动定时入库。
- Cookie 续签的旧占位实现已删除，失效时明确报错。生产由受控运维更新 Cookie，不执行未经验证的自动登录协议。

## 切换前还需验证

1. 按 README 配置 ERP 实际数据库、服务 token、有效 DH-Order Cookie、规则及允许访问的商品图片域名。
2. 网络恢复后完成 Docker 构建，并在 Linux/Python 3.12、ERP Node 20 环境验证。
3. 先运行影子/预览，同口径对账最近日期和历史边界；重点核实上游日期过滤含义、按 ID 查询行为、图片件数回退和日期口径。
4. 停用对应旧自动采集/JSON 导入写入器，然后开启 SAVEB_PUBLISH_ERP=true 并创建新任务。旧任务不会因为配置变更而自动变成正式发布。

## 重跑命令

```powershell
# 必须使用新建的独立测试实例；测试会清空 collector_test 内的测试表。
$env:COLLECTOR_TEST_DATABASE_URL='postgresql://USER:PASSWORD@127.0.0.1:PORT/collector_test'
$env:COLLECTOR_TEST_REDIS_URL='redis://127.0.0.1:PORT/1'
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check --config pyproject.toml app scripts tests
```

ERP 相关测试：

```bash
pnpm --dir api run build
pnpm --dir web run build
node --test api/tests/collector.test.js api/tests/route-registration.test.js api/tests/frontend-lifecycle.test.js
```
