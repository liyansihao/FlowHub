# 待补资料自动清理

已启用 Codex 当前任务的 heartbeat 自动任务 `flowhub-3`（FlowHub 待补资料自动清理），每5分钟执行一次本地脚本 `scripts/cleanup_stalled_repairs.py`。

规则：只处理启用中的FlowHub活动所属needs_fields任务。连续失败3次，或已有失败且从首次补全等待超过30分钟，转为needs_review。队列达到36条时，已失败至少2次且等待10分钟的旧任务可提前转出，直到低于36条。未尝试任务、活跃租约、其他状态均不操作；seed暂停时整轮跳过。

清理与队列认领使用同一SQLite写事务，迁移前写repair_cleanup_receipts保留原body、due、attempts和state。保留原offer、发布journal及价格；不删除毛子收藏/草稿、商品或改变库存。不是定时重启worker，也不绕过实际资料与下架检查。

验证：27项相关测试通过。首次执行成功，无达到条件的任务（parked=0）。正常清理不通知，仅异常或持续堵塞时提醒。
