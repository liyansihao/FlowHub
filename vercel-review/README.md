# FlowHub Vercel 审核台

生产网址：https://flowhub-review.vercel.app
账号项目：liyansihaos-projects/flowhub-review。
数据库：Neon flowhub-review-db，free_v3，新加坡 sin1。

2026-09-16 已导入 5187 件商品、1356 条本机已执行审核记录，迁移核对时待审核 943 件。默认队列只显示 needs_review，已处理商品移出。本机 data/review-sync.env 已切到 Vercel；数据请求通过现有代理。以下数量为首次迁移时的记录，实时数量随流水线变化。

旧 Cloudflare 网站已停止接收写入并跳转新站，原数据库未删除。其免费额度耗尽期间，无法读出尚未同步到本机的指令；这些指令保留在旧数据库，**尚未迁移**。原同步配置和本机快照备份在 data/vercel-migration-20260916/。旧服务保留同步凭据保护的 GET 查询用于后续核对；恢复读取后必须按 revision/动作/本机记录去重，不可直接重放全部历史。

部署：本目录 npm run build，然后 vercel deploy --prod。构建复用 ../netlify-review 的网页，API 使用本目录 lib/service.mjs 与 lib/repository.mjs。REVIEW_SYNC_TOKEN 与 DATABASE_URL 是生产环境变量，不提交密钥。只上传本目录应用文件。Git 自动部署已断开，避免误部署仓库根目录。

## 2026-09-16 省额度更新（已部署）

存储改为 PostgreSQL 的 flowhub_review_meta、flowhub_review_products、flowhub_review_decisions。数据库按条件筛选并分页，默认每页12条；不再每次取出整个商品快照。审核写入使用串行事务，保留版本校验、重复提交防护和已完成状态保护。历史记录持续保留。

本机采用 delta-v1：首次完整同步建立游标，之后只传变化商品及新增历史；内容未变化时跳过上传。收到服务器相同游标确认后才推进本机检查点，超时不会提前推进。传输仍采用 gzip-base64。配置为 FLOWHUB_REVIEW_PROTOCOL=delta-v1，FLOWHUB_REVIEW_SYNC_INTERVAL=30，FLOWHUB_REVIEW_IDLE_MAX_SECONDS=600。

用户已选择省额度模式：无人查看且无待执行指令时，后台检查间隔逐步增加到600秒；检测到查看者或待执行指令后恢复30秒基础间隔。周期还包含网络、生成快照和处理耗时，因此10分钟指的是空闲轮询等待上限，不是审核执行完成保证。网页隐藏时停止自动刷新，可见且内容未变时逐步放宽至5分钟；ETag未变化返回304，避免重复传输页面内容。

本机诊断文件：data/review-delta-metrics.json（上传计数和字节数）、data/review-poll-state.json（查看者活跃状态和待执行数量）、data/review-delta-checkpoint.json（确认游标及内容哈希）。不要公开同步配置或数据库连接文件。

验证：Python同步相关14项、前端轮询2项通过；真实Neon独立测试schema验证1500条合成商品的分页、筛选、历史、ETag、并发重复提交、版本与租户校验、增量重试及失败回滚。测试schema已清理，未使用真实商品动作测试。生产实测：原完整快照10,002,499字节，12条分页查询结果25,224字节，减少99.75%；重复HTTP查询返回304且正文0字节。这些为序列化数据量，不是供应商账单计量。记录见 reports/review-quota-optimization-20260916/。

上线观察：首次完整上传后已完成32次增量上传，最近一次仅1条变化、1889字节（压缩前），待执行指令为0。免费额度仍有限，不能用这段观察保证整月不超额；未升级付费套餐。

## 数据保留与回退

旧 flowhub_review_blobs 表保留。结构化迁移时导入5245条商品、1588条决定/历史，部署后又核对补齐11条旧格式记录，核对时遗漏0条。历史界面按已有规则去重，显示数量不等于物理记录数。

旧格式表在新版本上线后不再接收新决定，不能直接降级到旧部署：回退前必须备份并协调结构化表中的新决定、终态及游标，避免丢失或重放审核动作。旧Cloudflare尚未恢复读取的记录仍是上文所述的迁移遗留项，不包含在本次Vercel数据库核对中。
