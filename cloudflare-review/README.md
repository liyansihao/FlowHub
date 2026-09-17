# Cloudflare 审核台

Cloudflare Workers 托管 `../netlify-review/public`，SQLite Durable Object 持久保存商品快照与审核指令。复用 `../netlify-review/review-core.mjs`，保证审核权限、版本校验、记录格式与原站一致。

不上传毛子账号或店铺凭据。只配置审核同步专用 `REVIEW_SYNC_TOKEN` 为 Worker secret。不要把 `.dev.vars` 或 `data/*review*.env` 提交到代码库。

## 本地验证

```
npm install
npm run dev
node tests/local-integration.mjs
```

集成测试需要 `.dev.vars` 内 `REVIEW_SYNC_TOKEN="local-migration-test"`，只向 localhost:8320 发送合成商品。

## 部署与切换

1. Wrangler 官方登录需要 account:read、user:read、workers:write、workers_scripts:write。
2. `wrangler deploy`，记录实际 workers.dev 地址。
3. FlowHub 根目录运行 `PYTHONPATH=. .venv/bin/python scripts/migrate_review_host.py --url https://实际站点.workers.dev/api/reviews`，只准备私密配置，不切换。
4. 把生成配置中的同步 token 通过 stdin 安装为 Worker secret，不放在命令行参数或日志。
5. 本机 worker 需要加载 remote_reviews v2（支持迁移互斥锁和配置热读取）。
6. 加 `--activate`：先验证目标快照，再停用旧站动作，导入所有远程历史及待执行指令，原子替换本机配置。失败时恢复旧站操作。
7. 回查新站数量、历史、待执行队列及自动同步时间。商品上下架不作为 UI 测试动作。

本机备份在 `data/review-migration-<timestamp>/`；Cloudflare 同步私密配置在 `data/cloudflare-review.env`。在新站验证成功前不停用旧站。

128个固定SKU桶及事务写入避免每次商品变化重写整个快照；只有变化的桶写入数据库。30秒同步一次。免费额度仍受Cloudflare平台实际用量限制。

## 当前网络连接

本机系统对新 workers.dev 域名的解析结果不正确。`flowhub/review_transport.py` 支持此审核域名专用 `FLOWHUB_REVIEW_NETWORK=proxy_doh`：通过 HTTPS DNS 解析，使用已配置代理连接正确地址，保留原域名的 TLS 验证；不修改系统 DNS、代理或浏览器设置。同步专用配置内保存 `FLOWHUB_REVIEW_PROXY`。该路径只修复本机同步，不能替代浏览器访问验证。

2026-09-15：新站 https://flowhub-review.flowhub-cloudflare-review.workers.dev/ 已部署，2744件商品已复制并暂时禁用动作。原站尚未停用，等待用户确认 Chrome 能打开新站后运行迁移激活。

迁移已激活：2026-09-15，用户确认 Chrome 可访问新站。2744件商品、410条历史记录可查询；9条远程记录转移，待转移指令0；旧站按钮禁用。自动同步时间已验证继续推进。
