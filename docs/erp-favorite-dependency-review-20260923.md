# ERP收藏业务依赖分析（先分析，不实施重构）

## 结论

不能证明“商品进入本地任务系统以后ERP收藏即可安全删除”，并且代码存在明确反例。本地任务/商品快照不是远端跟卖导入所需的收藏实体替代品。本轮停止在分析阶段，不设计或部署基于该前提的新清理逻辑。

分支codex/erp-favorite-dependency-review-20260923从main ac08a4d952dce250b2e46d6b6779e041c57cd095创建，没有带入现有清理候选或网络候选。生产仍93731b5；生产基线核对通过，无源码/配置/数据库修改，没有新增删除实验。暂停旧自动优化/验收心跳，不代表已停止生产worker或现有清理任务。

## 实际调用链与反例

当前新上架策略maozi_follow：plugin_publication._advance先把snapshot/plan/offer写进plugin_publications（flowhub/plugin_publication.py:251附近），之后仍创建PublicationAdapter和ProductionListingService（:287附近），调用recover_favorite（:299附近）及发布状态机。因此即使本地资料已保存，后续依赖仍存在。

- flowhub/pipeline_modules/favorite_lookup.py:36-37：生产适配器继承FlowEF的MaoziProductionAdapter。这里是唯一生产实际引用的库，不是启动旧FlowEF发布服务。
- FlowEF-production/src/flowef/application/services/test_listing.py:101：prepared/favorite_pending/ready每次查favorite_id；:141-158在ready->submitting时把该ID传给publish_zero，明确拒绝后回ready继续依赖收藏。
- FlowEF-production/src/flowef/adapters/erp/maozi_test_listing.py:114-129：publish_zero构造并发送/api.selection.follow/import。
- FlowEF-production/src/flowef/adapters/erp/maozi_contract.py:185-197：提交rows包含id=favorite_id、source=favorite。不能以本地sku/title/图片均存在就删掉远端实体。
- flowhub/pipeline_modules/favorite_recovery.py:19-25：ready时收藏消失会转回favorite_pending；:58-79有等待、最多3次恢复、source_already_imported门禁及重新收藏。重新创建不是“删除无影响”的证明，反而说明有恢复成本和失败路径。

## 七个业务阶段

| 阶段 | 是否依赖收藏 | 证据和删除边界 |
|---|---|---|
| 等待上架 | maozi_follow仍需要 | prepared/ready随后要查询并提交favorite_id；本地已入队不释放依赖。尚未创建收藏的任务也会先创建。 |
| 正在上架 | 需要或尚不能证明释放 | favorite_pending/ready直接依赖；submitting已发请求可能结果未知，不得把本地submitting理解成Ozon已接收成功。 |
| 已提交Ozon | 须区分真实确认和仅ERP请求返回 | 状态机:161-188通过shop+Offer查询商品、查import_status、同步商品，无直接favorite_id参数；但源码不能证明ERP内部异步作业已脱离收藏，也没有删收藏后异步导入完成的远端契约证据。仅HTTP成功或reconciling不足以放行。 |
| 等待审核 | 已独立生成商品的只读审核路径不直接使用收藏；不能一概安全 | :190-235按product/Offer检查平台问题和状态；plugin_publication.py图片修复使用/api.product.online/repair_images和ERP商品record_id。若后续转资料重采/重新导入，依赖可能重新出现。不能只按界面“审核中”分类。 |
| 补资料 | 部分明确需要 | repair_source.py:42-45调用SourceCollector。source_detail.py:348-360拿favorite_id调用favorite/edit_import创建草稿；acquisition.py:528-537相同。已有有效快照或draft_id的路径可直接读取资料，但不是全部补资料任务。 |
| 失败重试 | 取决于失败阶段，不能统删 | 导入明确拒绝回ready，需要收藏；favorite_pending及未知收藏写入靠精确查询核对；提交后的未知结果按原Offer核对，不盲重发，但安全删除时间仍未证实。 |
| 后续重新处理 | 可能再次需要 | source_detail.py:246-269只在快照有效或草稿仍可读时复用；草稿已被回收时重建，重新进入收藏查找/edit_import。repair_source.py:42允许existing_favorite_only；此时无收藏且无真实价格会等待，不能默认总能重建。 |

## 补资料的缓存边界

source_detail.py:246-249仅复用6小时内ready资料；:262-270有draft_id则直接读/api.product.collect/detail，不直接依赖收藏。:254-261如果草稿已删除，则返回重新采集路径，:307-360会查/建收藏并调用edit_import。acquisition.py:472-498通过收藏查找恢复，:528-537创建草稿。故“当前一次回查不访问收藏”不等于“该商品所有后续流程不再访问”。

历史官方直发不同：official_publication.py:310-315的favorite_id是local-dossier摘要，占位符不等于ERP收藏ID；真正提交走官方接口。但历史去重guard仍有读取ERP收藏/导入日志路径（official_source_guard.py:36-71），资料重采依赖也需单独判断。不能将官方路径性质套用到当前maozi_follow。

## 验证证据及局限

未新增测试代码，仅运行main已有tests/test_follow_publication.py与tests/test_favorite_recovery.py，34项通过（existing-tests.txt）。包括：已有本地输入仍以source=favorite导入；收藏缺失先创建并观测再提交；ready收藏消失回恢复路径；未知导入不重复发出。这些使用模拟远端，只证明代码依赖及恢复行为，不证明真实ERP删除对异步导入的副作用。

生产库只读快照production-states.json：maozi_follow有prepared6、ready1、submitting15、reconciling12、manual_review403；source_details有favorite_started173、draft_started118、draft_rejected269。这是混合历史/当前状态，不把它们全算当前活跃或可清理数量，也不是确认所有对应远端收藏仍存在。它说明不能只凭本地任务存在或一个大类状态宣布依赖全部解除。

main与生产差异只涉及既有网络调度/收藏清理及测试；这里引用的发布适配、资料采集、收藏恢复核心文件未在候选中改动。

## 本轮停止点

已找到明确依赖，按照用户要求停止实现。尤其不能对所有“本地已有任务”快速批量删除。以后若继续设计，必须先选定可证明的依赖解除边界，并取得ERP异步导入的服务端契约或受控证据；否则只能使用已证实完成且无重采/未知写入依赖的范围。不能由清理模块自行取消补资料/重试能力以扩大删除数量。
