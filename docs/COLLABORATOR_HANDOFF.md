# 协作者交接：FlowHub 固定基线

更新日期：2026-09-17。协作基线为 `runtime-20260917.1`（`fbf2487`），完整开发步骤见 [CONTRIBUTING](../CONTRIBUTING.md)。GitHub main 的文档更新不改变该标签指向。

## 本次交付范围

本版保存主 FlowHub 目录已存在的来源发现、资料补齐、同款审核、人工审核、发布回查、队列隔离、多电脑计算、审核站点和 Windows agent 源码，并补上审核表尚未初始化时保持人工审核的保护。包含模块不表示每项功能都已启用或完成现场部署。

固定版验证：713 项 Python、44 项 Node 测试通过。账号、密钥、数据库、浏览器会话、模型和运行记录没有进入仓库。外部依赖另有源码快照与哈希清单；仅克隆 FlowHub 无法复现完整生产环境。详见[固定版说明](FIXED_RUNTIME_20260917.md)。

固定版本没有合并 `codex/ozon-official-api` 的全部历史提交，也没有包括固定版之后 `codex/repair-publication-throughput-20260917` 的开发提交。后续改动需单独审查、测试和合并。生产机器可能使用其他分支或旧进程，GitHub main 不等于现场当前运行状态。

## 从哪里读代码

| 模块 | 入口 | 职责 |
| --- | --- | --- |
| 总调度 | `flowhub/worker.py`、`pipeline_modules/runtime.py` | 独立来源、补资料、审核、提交、回查通道 |
| 种子与整店来源 | `pipeline_modules/source_loop.py`、`browser_source.py`、`bridges/playwright-source.mjs` | 精确来源绑定、持久会话、轮转、暂停、断点、重试、复采 |
| 其他卖家与多代探索 | `other_sellers.py`、`bridges/other-sellers.mjs`、`discovery_facts.py` | 独立发现种子、真实卖家弹层、采样过滤、父级证据与generation；直连事实保留原统计周期 |
| 候选录取 | `pipeline_modules/admission.py` | 新旧候选交替、有限验收集合优先、SKU去重、队列上限、目标路由与授权范围 |
| 资料补齐 | `pipeline_modules/direct_facts.py`、`pipeline_modules/repair.py`、`source_detail.py` | 详情直读与会话复用、逐字段补齐、必要时草稿兜底 |
| 1688/DINO/千问/利润 | `plugin_comparebot.py`、`comparebot_process.py`、`vendor/compareBot` | 供应商匹配、常驻模型、审核、成本和利润证据 |
| 发布 | `plugin_pipeline.py`、`plugin_publication.py` | 持久发布意图、提交、低频回查、库存与可售确认 |
| 审核同步 | `pipeline_modules/dossier.py`、`plugin_pipeline.py` | 关键资料不变时同步已补齐资料，审批过期或经济条件变化则重新测算 |
| 人工审核 | `manual_reviews.py`、`web/assets/app.js` | 报告版本绑定、三种操作与审核日志 |
| 旧草稿清理 | `pipeline_modules/draft_cleanup.py` | 精确归属、加密备份、删除意图与完整列表回查 |
| 页面与接口 | `source_api.py`、`web/assets/app.js` | 沿用现有界面与用户权限；部分新控制仍通过CLI完成 |

表中未写完整目录的Python文件均在flowhub下。旧seed_pipeline、hour_acceptance等固定批次模块保留作历史参考，直接运行入口已停用；不要恢复历史硬编码授权。


## 补充模块入口

| 模块 | 入口 | 协作重点 |
| --- | --- | --- |
| 同款识别与人工决定 | `flowhub/identity_review.py`、`flowhub/manual_reviews.py` | 精确商品身份、证据版本、人工作用范围 |
| 官方接口与隔离恢复 | `flowhub/official_*.py`、`flowhub/pipeline_modules/isolation.py` | 接口绑定、未知结果回查、与既有 ERP 路径兼容 |
| 多电脑执行 | `flowhub/cluster*.py`、`packaging/windows-agent/` | 任务归属、租约、凭据边界、失联恢复 |
| 远端审核 | `flowhub/remote_reviews.py`、`vercel-review/`、`netlify-review/`、`cloudflare-review/` | 用户隔离、同步版本、服务端凭据保护 |
| 补齐与发布排期 | `flowhub/pipeline_modules/repair*.py`、`runtime.py` | 有界重试、资源限制、发布意图与回查 |

## 建议的协作任务

1. 将相邻工作区依赖逐步整理为可安装、可验证的组件，优先解决干净 clone 无法执行完整测试的问题。
2. 为来源分页、会话失效和上游格式变化补充脱敏回归样例，保持断点与精确身份绑定。
3. 完善补资料、人工审核、提交接受、确认可售的独立状态展示与观测。
4. 按模块审查固定版之后的官方发布和吞吐修复分支，避免一次合入不相关实验。

## 不可混淆的验收口径

新增不重复候选、测算通过、提交接受、确认可售分别记录。短时吞吐样本不能证明长期稳定速率，测试通过不能代替真实发布验收。真实业务结果保存在私有持久审计，不将现场数据放入公共 fixtures。

合并走独立分支和 PR；部署由维护者按[流水线发布说明](PIPELINE_RELEASE.md)执行。保留持久发布意图与未知写入记录，不通过删除队列、重放提交或覆盖数据库解决状态问题。
