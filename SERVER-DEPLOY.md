# 当前部署入口

当前运行组合是 saveb-admin + saveb-api-go + saveb-collector。

三个项目放在同一父目录，在本项目执行 `bash start.sh` 会转到 saveb-api-go 的统一启动脚本，构建并更新三个项目。首次选择环境可执行 `bash start.sh local`、`bash start.sh test` 或 `bash start.sh production`；后续记住所选环境。

环境变量来自本项目的 `.env.local` / `.env.test` / `.env.production`，直接随内部 Git 仓库维护；测试/生产中未填写的真实值仍需补齐。

完整步骤、数据恢复、端口、Nginx、生产 HTTPS 入口及限制请看 ../saveb-api-go/DEPLOYMENT.md。旧 RELEASE_IMAGE 自动发布脚本不作为当前启动入口。
