# 旧草稿自动清理

配置：data/draft-cleanup.json。enabled 为启停开关；暂停种子模块或关闭活动 campaign 同样阻止删除。后台 pipeline runtime 独立运行，每 300 秒检查一次，使用 draft-cleanup.lock 避免重复执行。

默认规则：采集箱使用量达到 85% 时清理，目标占用 80%；每次每个账号最多 20 个。源资料至少保存 1 小时，必须是本 FlowHub 创建（存在收藏创建记录、源 SKU、草稿 ID 和完整快照，排除仅通过恢复查找到的草稿），且本地对应任务已 selling。清理前重新核对当前账号采集箱的草稿 ID、goods_id=源 SKU、来源 ozon，以及指定店铺和 offer_id 的线上在售且有库存状态。

删除前备份实时草稿内容、原采集快照、精确线上商品证据与草稿列表记录。备份使用本工作区数据库密钥加密，保存在 draft_cleanup_receipts.body。状态依次为 intent、acknowledged、deleted；超时或未知错误为 unconfirmed。已有操作记录不自动重复删除，只有完整且一致的采集箱分页列表证明目标不在其中，才记作 deleted。分页期间数量变化或出现重复造成不完整时，回查失败并保留待确认记录。

清理只调用 /api.product.collect/del，参数仅含精确草稿 ID，不调用线上商品删除、库存或归档接口。不清理待审核、测算失败、上架未确认、资料不完整或无归属证据的草稿。没有合格旧草稿可清理时记录 no_safe_candidates，不扩大删除范围。

每轮摘要保存在 pipeline_module_events，outcome=draft_cleanup；包含占用、限制、删除数量及跳过数量，错误只记录异常类型。详细回查与备份见加密收据表。

2026-09-14 首轮真实验收：清理 3 个草稿并确认全部不存在；期间新采集 1 个，使用量从 1000 到 998。报告 reports/draft-cleanup-20260914/live-acceptance.json。正式配置已恢复每批最多 20 个。
