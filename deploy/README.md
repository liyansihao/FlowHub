# 统一服务部署

状态：部署配置已准备；服务器地址与域名尚待提供，未上线。

客户登录体验：安装包预设同一 HTTPS 地址，客户输入现有账号密码后看到账号绑定的店铺。原账号密码哈希、店铺加密资料与 master.key 一并迁移，不让客户重复绑定。

## 部署顺序

1. 在用户指定服务器上确认 SSH、Docker Compose、磁盘以及现有 80/443 服务。不能覆盖服务器已有网站；若已有反向代理，接入已有代理而不启动这里的 proxy。
2. 将域名解析到该服务器，准备 HTTPS。Caddy 自动申请、续期证书需要域名解析、80/443 可达和持久化证书目录。[官方说明](https://caddyserver.com/docs/automatic-https)
3. 原服务暂停新增、在途任务回查完成后，使用 `prepare_migration.py --source 原data目录 --output 新的私密目录` 制作一致快照。该工具不更改原数据库，不复制现有登录会话，不自动启动任务。
4. 通过 SSH 传输代码与快照到服务器，数据目录归容器运行用户 UID/GID 10001 所有、目录 700、文件 600。不要将账号数据放入镜像、源码压缩包或安装包。
5. 复制 `.env.example` 为 `.env`，填真实域名与数据绝对路径；运行 `docker compose --env-file .env -f compose.yaml up -d --build web proxy`。该配置仅公开 HTTPS/HTTP 入口，数据库没有公开端口。
6. 通过 HTTPS 验证 healthz、登录、同账号两个独立会话的店铺一致性，以及其他账号隔离。
7. 迁移并验收上架依赖后，保证原执行器停止，才启用 execution profile 的单个 worker。不要同时运行源端和迁移后的执行器。Docker profile 用法见[官方文档](https://docs.docker.com/compose/how-tos/profiles/)。
8. 核验备份恢复、重启恢复、候选获取、利润及店铺只读连接；保留原提交上限。真实上架任务由用户在新入口启动。
9. 将桌面 `client/workbench-config.json` 的 serviceUrl 设置为已验收的 HTTPS 地址，再重建安装包并实测登录。旧版本本机地址不能冒充已配置的共享服务。

## 本项目特有依赖

当前管理员工作流使用 flowb 候选/匹配、毛子发布与 Feishu 禁止上架检查，不能只搬 Python Web 就认定上架完成。通用 Dockerfile 不含 Node 或旧 FlowEF 代码。拿到目标服务器后，需按已选模块迁移 Node 桥接脚本、Python 检查器、资料、SKU 占用记录及对应凭据，或部署已核验的远程模块；保持账号密钥隔离和明确下架保护。

目前 execution profile 默认关闭是为了先验收账号迁移，不代表部署完成。迁移回滚前也必须先停止新执行器并完成在途回查，防止同一商品重复提交。

日常备份应使用 SQLite backup API 与配套 master.key 保存为权限受限的一组文件，并保留服务器外副本。prepare_migration 是停机切换工具，不用于运行中周期备份。
