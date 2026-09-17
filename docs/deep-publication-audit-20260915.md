# 上架链路深度检查（2026-09-15）

## 已修复

1. plugin_pipeline：awaiting_remote 的临时连接错误累计到8次后会被移到 needs_review，失去自动回查。现在保留远端回查队列，仍遵守退避和原 journal；没有重放未知导入请求。
2. favorite_recovery：favorite_visibility_exhausted 原来停止一切自动恢复。现在每15分钟只读检查一次；迟到收藏可恢复 ready 并重新执行原发布前检查，但不会增加已耗尽的收藏写入预算。
3. plugin_publication：收藏恢复逻辑位于旧错误记录范围之外，异常时 phase/timings/event 会缺失。现已补齐持久化错误记录，便于下一次定位。

## 运行数据与验证

- 恢复48条旧任务：11条因临时网络错误停止的超时任务、37条收藏次数耗尽任务。只调整回查调度，原 offer、journal、价格/利润/审批/下架排除规则及写入次数保持原值。
- 变更前逐条快照：reports/deep-publication-audit-20260915/recovery-before.json。
- FlowHub 73项、FlowEF 63项相关测试通过，共136项。覆盖迟到收藏、耗尽后不追加写入、未知请求不重放、回查异常持续恢复、15分钟退避、并发锁、清理限制及发布保护。
- 核查时未发现重复来源 journal、重复店铺货号、下架控制与活跃发布冲突、无租约任务的队列/journal阶段不一致，或活跃来源落入飞书明确下架SKU清单。
- 排空后重载主 worker；原采集、审核、发布暂停开关均恢复为未暂停。

## 尚未解决的外部/商品问题

历史 journal 有57条 platform_issue_requires_review，不能视为均已恢复。独立抽查：
- 3428790124：ready_to_sell / stock0 / warning_attribute_values_out_of_range。
- 1689690125：SKU0 / unknown / DESCRIPTION_DECLINE。
- 4852866007：SKU0 / not_selling / error_attribute_values_out_of_range。

这些需要按商品属性或图片问题处理，未在本轮放宽平台错误保护。收藏曾在短时间内消失的最终删除来源仍未被客户端证据确认，本次修复的是可证明的本地恢复缺陷，不能当成该远端现象已根治。
