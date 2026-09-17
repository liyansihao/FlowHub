# FlowHub 外部代审核台

这个目录是 Netlify 前端和一个 Netlify Function。网站只保存待审核商品的脱敏快照和审核决定，不包含 FlowHub 数据库、毛子 ERP 凭据、千问密钥或本机登录 Cookie。审核台不要求密码；任何拿到网址的人都可以提交同款确认、拒绝或补资料决定，适合交给代审核人员使用。

当前生产站点：<https://deluxe-sunflower-0f9ca8.netlify.app>

## 部署前

在 `netlify-review` 目录运行：

```bash
python3 scripts/export_reviews.py
```

在 Netlify 环境变量中设置一个随机值：

- `REVIEW_SYNC_TOKEN`：只放在本机 FlowHub 环境中，不能发给审核人。审核台本身公开访问，不要求密码。

部署后，把 `https://你的站点域名/api/reviews` 写入本机环境变量：

```bash
export FLOWHUB_REVIEW_SYNC_URL="https://你的站点域名/api/reviews"
export FLOWHUB_REVIEW_SYNC_TOKEN="与 REVIEW_SYNC_TOKEN 相同"
export FLOWHUB_REVIEW_OWNER="31f8fc715a2766c1ecb6bd96"
```

然后重启 FlowHub worker。worker 每15秒拉取一次决定：确认同款并通过本地检查后会进入原有 `publishing` 队列，后续继续毛子 ERP 的上架与回查；不通过会进入 FlowHub 的 `rejected` 终态（仅移出本地待处理队列，不代表平台归档）。通过不代表平台已经成功上架，原有库存、禁上架清单、利润和回查保护仍然有效。当前主机的同步配置保存在被忽略的 `FlowHub/data/review-sync.env`，电脑重启后仍会由 supervisor 自动加载。

worker 会每15秒把当前 `needs_review` 队列推送到 Netlify，因此网站不是固定快照；首次部署前仍可运行导出脚本作为兜底数据，之后本机实时队列会覆盖它。


审核卡片区分人工同款复核、缺资料、测算过期、发布异常，列出最新来源资料的缺失字段与最近补采失败步骤。批准仍校验报告本身；“上架商品属性”只表示属性列表存在，不表示已经验证必填属性 ID。

“补资料 / 刷新测算”保留旧报告与完整审计，设置 `repair_full_dossier=True` 并进入 `needs_fields`。后续修复流程仅在关键数据不变且原测算仍有效时复用同款判断。此操作不续期、不人工改判、不直接批准。已提交平台或已有发布记录的商品须回查原记录，禁止通过 repair 重建发布。

POST 仅使用本机推送的 current snapshot；seed 只供初始展示。导出脚本复用本地卡片与 revision，且只读数据库，必须显式设置 `FLOWHUB_REVIEW_OWNER`。审核理由必填。

快照与各审核决定分别存入 Blobs，避免 refresh 覆盖审核或 ack。决定绑定租户、商品和 revision，旧版本历史不阻挡新版审核。并发审核最终由本地数据库事务裁决；回执丢失可幂等重放，不重复修改队列。业务校验拒绝为终态，临时同步错误仍待重试，处理后立即推送新快照。

本地验证（不部署、不操作实际队列）：

```bash
node --test netlify-review/tests/reviews.test.mjs
.venv/bin/python -m pytest tests/test_manual_reviews.py tests/test_remote_reviews.py tests/test_repaired_review_sync.py -q
```
