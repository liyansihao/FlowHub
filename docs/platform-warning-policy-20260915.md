# 平台非阻塞警告放行修复（2026-09-15）

## 修改

毛子商品观察保留完整平台错误（code、level、state、texts）及当前主图地址。初次上架回查、库存最后写入检查、库存后可售回查统一使用同一分类逻辑。

只有全部提示明确为 WARNING、没有 declined/rejected/failed 状态、平台状态为 ready_to_sell/selling/out_of_stock 且有 HTTP(S) 主图地址，才不因这些提示阻断。主图地址来自平台商品记录，不代表本地已逐张下载验证。ERROR、未知等级、只有历史错误码、无主图、整套图片失败、重复商品和缺图强制错误仍拦截。继续执行已有SKU、售价审核、商品归属、店铺、库存、飞书明确下架等检查。

旧 platform_issue_requires_review 记录再次运行时先查精确店铺+原货号的最新状态，再跑原preflight，满足条件才转reconciling。保留原offer、原价格与截止时间；不重建商品、不重置不明库存写入。

## 验证与上线

- FlowEF相关测试78项通过（含警告库存写入/可售回查、错误阻断、库存最终检查）。
- FlowHub相关测试63项通过（含旧任务恢复、过期授权、下架拦截、身份变更）。
- 57条真实接口快照重放：22条通过警告判断，35条继续阻断。
- 已安全排空后重启worker：39118 → 55800，seed/review/publication均恢复运行。
- 实时读飞书下架排除表，并检查本地block/unlist及租约后，22条旧任务已重新进入awaiting_remote队列。保留原journal阶段，由工作流执行实时回查与校验。
- 重新入队不等于已上架。部分旧记录审核/操作时限已过期，仍需走原有刷新流程。

证据：../reports/platform-57-root-cause-20260915/；迁移快照warning-recovery-before.json。
