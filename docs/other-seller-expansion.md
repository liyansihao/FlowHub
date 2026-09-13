# 商品种子发现其他卖家

本模块补充 source_loop 原有“解析当前卖家”的能力。商品可以在上架、订单产生之前加入发现队列：`other_sellers.replenish` 从来源库读取商品，按当前 `SourceFilters` 通过后写入 `source_discovery_seeds`，不伪造本店 offer，也不写入自有商品种子表。未通过的检查结果写入 `source_discovery_checks`，缺项保留；一小时后可再检查。

Playwright 专用持久化会话打开种子商品实际 `webBestSeller` 入口，点击其他卖家弹层，仅读取 `webSellerList` 内卖家链接。页面 modalLink 的 product_id 必须等于种子SKU。推荐区和当前卖家区不计入该列表。

只有去重店铺数与页面明确标示数量相等才记录 complete。数量缺失、不一致或滚动预算耗尽都记录 partial，保留全部关联证据，下一次重新加载并扩大滚动预算（最多40轮）。这是可重复合并的部分读取，尚未实现服务端分页游标续读；不声称静止页面就是末页。

每个 `(owner, seed SKU, sellerId)` 单独保存关联。新 sellerId 进入一页采样评估；采样复用 BrowserSource 的真实店铺ID、商品网格、分页及去重验证。入口SKU不会成为新店铺的商品SKU。至少一个采样商品通过现有来源过滤，才加入持续整店轮转；缺资料或不合格的原因保存在评估记录。新店铺不因“被发现”就获准发布。

调度在现有 source-loop profile OS锁内运行。发现每120秒至多一个SKU，采样每轮一页；原有店铺每轮3页。网络失败有界退避，人工暂停不被采样选择器解除。待处理种子限制500；候选积压达到原阈值时停止新店采样及整店读取，卖家发现仍可有界运行。配置开关为 `other_sellers_enabled`，全局 seed 暂停仍有效。

原始31个候选验收采用首次 validation.json 的时间边界固定SKU集合。campaign 的 `acceptance_skus` 最多100个，优先于新旧混排，但仍受并发、批次总量、去重及下架排除约束。避免该批次夹在持续新增与历史积压之间而长期不被录取。

## 真实证据

2026-09-13：原有合格种子样本种子A的其他卖家弹层明确显示12家，实际读取12家，全部此前不存在于本机来源扫描库。新店铺样本店铺A采样一页新增8个不同SKU，页2游标保留；入口样本种子A不在这8个SKU中。另一家样本店铺B导航ERR_FAILED，保留失败及页1，没有标记采完。

这次证明的是“已有合格种子→其他卖家→新店铺→不重复商品”。本段为首轮验收，当时尚未把新采样商品通过来源筛选并自动补成第二代种子的真实结果计为已完成；后续探索结果见下方“第二代扩展验收”。来源过滤仍需均价、销量、类目、跟卖状态等实际数据，不能用发布成功冒充来源条件满足。

逐条验收文件保存在本机 `reports/other-seller-expansion-20260913/cohort-status.json`、`acceptance.json`；原始浏览器证据在 `output/playwright/other-seller-expansion/` 及新店铺扫描目录。执行 `PYTHONPATH=. .venv/bin/python reports/other-seller-expansion-20260913/cohort.py` 可刷新原始31个候选的下游状态。

执行器实测修正：Ozon初始SSR容器可存在但没有可见尺寸，且模态点击事件需等待初始化。按原项目等待和滚动顺序修正后，`bridges/other-sellers.mjs` 通过 `discover_one` 再次读到12家，返回 complete、new_sellers=0。此前失败证据保留。完整性结论仅针对页面该弹层明确标示的12家，不宣称覆盖平台所有潜在卖家。38项相关测试通过。

## 第二代扩展验收

用户明确允许“先探索，保留待筛选状态”后启用 `explore_pending_sources`。该开关仅允许具有其他卖家来源链、标题、图片和入库证据且没有明确筛选失败的待补齐商品成为探索种子；assessment 原样保留，`exploration_only=true`，不解除上架审核或店铺持续采集的来源过滤。每代种子保留父级根证据和 generation。

第二代8个探索种子中，3个已完成其他卖家列表读取，新增8家店铺。沿第二代样本种子发现的第三代样本店铺A、第三代样本店铺B分别采样1页，共新增16个不重复SKU，16个商品已补入第三代探索种子队列。来源持续扩展已出现真实下一代商品，不代表这些新商品均已通过商业筛选。

同时修复 CompareBot 对所有根证据强制读取 shop/offer 的 KeyError：其他卖家发现根以真实 seed SKU、页面hash和包含当前来源 sellerId 的卖家列表校验；仍排除明确下架SKU。已有本店offer根仍检查店铺及offer下架记录，未伪造第二代商品的自有offer。原8商品复跑后3个确认上架、3个利润不足淘汰、2个待千问复核。

毛子 sku3 新查到的销量、销售额、类目、重量和FBS按实际语义保存；未声明周期的数据不会写成28天统计，未返回跟卖许可则保持未知。逐条证据见本机 `reports/second-generation-20260913/acceptance.json`。
