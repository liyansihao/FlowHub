# 持久选品流水线 v3

## 实际运行路径

主 worker 启动 admission、补采、CompareBot 审核、发布和回查通道。`pipeline_campaigns` 持久化授权、目标店铺、最大在途数和定价策略。默认关闭，用户授权后启用。生产配置为在途上限12，依赖正常后持续供给。不是定时重跑一次性脚本。

admission 从有精确卖家及种子关联的来源库选未处理商品，仅接受 storefront-page/maozi-exact-seller-page 覆盖；排除已有发布队列、任务和屏蔽项，按来源SKU去重。来源可由持久化 Playwright 轮转采集器补充，参见 source-loop.md；admission 本身不启动浏览器。每件先补资料再测算，沿用既有利润和飞书排除规则。

选定售价是新的经营定价意图，可沿用原来源页的 CNY/RUB 价格，原始观察时间与原币种保留。RUB 转换使用毛子实时汇率。不会把旧市场价改成“刚刚采集”。审核和包装超过有效期必须重读/重算。

## 内部卡点修复

- 不再依赖 seed_pipeline/hour_acceptance CLI 的独立截止/进度库；旧浏览器 CLI 已停用。来源账户 own_orders/own_shop 的只读增量已开启，无榜单替代。
- 队列准入有原子去重和在途上限；租约续期、暂停和重启保留断点。
- 连续campaign可续接自己的发布窗口并留历史，不能静默延长其他历史任务的截止。
- 重新批准采购方案后，可版本化变更内部供应商/成本；旧方案完整留档，外部offer、售价、包装必须完全一致。提交结果未知时不重发。
- 20分钟超时进入 awaiting_remote，低频查询精确offer。晚到的可售/库存99结果可自动恢复；没有有效授权或近期审核不能补库存。
- 毛子官方前端确认的 repair_images 接口用于纯图片失败。先记意图再提交一次，响应丢失不重发，等待平台回查；明确类目/内容问题不会因此被放行。
- 同一ERP凭证的店铺列表短时复用15秒；写入使共享缓存失效。在线商品和库存仍不缓存。
- 有独立Seller API密钥并验证仓库归属的店铺可启用 official_observations。已验证一个账户；其他目标店铺没有可用独立密钥，保持毛子回查。
- 主工作流已接入用户已有CompareBot千问密钥，保存在加密配置，审核缓存包含凭证版本。欠费、鉴权失败与商品不匹配分开处理。
- 守护进程清理旧进程组的PermissionError不再导致整个守护退出。本工作区只启动worker守护，不占其他实例网页端口。
- status统一展示active_run_id、campaign、能力阻塞、队列年龄、最近10分钟无可售回查提示。

## 真实验收与未完成条件

2026-09-12 首批3件均自动完成资料补齐与CompareBot测算，其中一件产出新商品，目标嘉兴邮政仓库存99、selling。另两件因上游审核服务不可用暂停，不是商品审核否决；已置awaiting_dependency。后续在途与回查以reports/continuous-v3-20260912/status.json为准。

外部阻塞：

1. 千问真实API调用返回Arrearage。已连接已有密钥，仍需用户恢复该账号额度。新入队已设置依赖熔断；恢复后执行下列重试命令，真实千问审核成功才解除新供给等待。
2. 竞品整店持续发现尚无可验证的无浏览器入口。Ozon原生接口复测403/captcha；毛子官方前端已检视selection/product/shop API，尚未发现精确竞品整店分页接口。不会把top/lists当整店。需要用户提供毛子整店功能的具体入口进一步核验。

因此这里只证明来源库→补采→测算→上架链路；还没有证明全新整店发现→持续多小时40件/小时。不能说所有卡点都已消失。

## 操作

```sh
.venv/bin/python -m flowhub.pipeline_modules status
.venv/bin/python -m flowhub.pipeline_modules pause seed
.venv/bin/python -m flowhub.pipeline_modules pause review
.venv/bin/python -m flowhub.pipeline_modules pause publication
.venv/bin/python -m flowhub.pipeline_modules resume seed
# 千问额度/配置恢复后，重新审核依赖等待项；不会绕过审核：
.venv/bin/python -m flowhub.pipeline_modules retry-dependencies
```

暂停不打断已发送写入。恢复后按数据库进度继续。缺少真资料、明确禁止、飞书明确下架、供应商绑定冲突和利润未过均不能自动上架。
