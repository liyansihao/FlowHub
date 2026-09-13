# 种子来源持续采集闭环

工作进程的独立 source_loop 负责来源发现与 Playwright 整店分页；原 admission、毛子补资料、CompareBot、审核和发布队列继续负责下游。配置保存在本实例 data/source-loop.json，默认没有配置时不启动。已为当前工作区启用，未修改桌面应用另一套数据库。

## 数据流

1. 已确认上架的记录，以来源 SKU、来源 seller、本店 shop 和 offer 的明确绑定补充 sourcing_seeds。已有种子不覆盖；自有商品和订单采集继续更新存续状态、销量。
2. 历史整店根证据可按来源 SKU、本店 shop、offer 精确补齐种子的来源店铺。未解析种子优先复用来源库的唯一精确页面证据，否则请求毛子 /api.chrome/sku3。必须返回相同 SKU 和明确店铺 ID；不从货号推导，不调用榜单。接口缺店铺 ID时记录 seller_missing_in_direct_response，6小时后可重试，不算发现成功。
3. 新店铺持久入队；重复发现不重置游标，后续同步不解除手动暂停。
4. 每店每轮3页，完成一轮后移到队尾。导航超时等暂时故障按退避最多自动重试3次；验证码、身份不匹配停止该来源，其他店铺继续。手动 browser_source retry 可恢复。
5. 到明确末页才标记整店完成，24小时后建立新扫描代次复采。旧页面证据和断点保留。不能用短页或重复页冒充采完。
6. 商品与下一页在同一个事务写入来源库；重复SKU不新增。原测算队列自动领取；启用 mix_fresh_sources 后新来源和旧积压交替领取。原500个录取上限、12个在处理上限、发布权限和明确下架排除继续生效。待录取来源达到2000个时暂停扩采，避免只堆数量。

## 运维与验收

- `python -m flowhub.pipeline_modules status` 的 sources 返回来源状态、种子解析状态和 Playwright 新候选的下游状态。
- `python -m flowhub.pipeline_modules pause seed` 会暂停来源采集及补资料。浏览器提交页面前再次检查开关，已在途但未提交的页面不推进断点。resume seed恢复。
- `python -m flowhub.browser_source pause|resume|retry --owner OWNER --run-id RUN --seller SELLER` 控制单次店铺扫描。当前 run_id可从source_loop_stores查询。
- 专用 profile 由单个轮转采集器使用；不要同时手动或通过CLI打开相同profile。数据库OS锁防止两个FlowHub采集器同时运行，浏览器窗口仍由Playwright有界启动和关闭。
- 来源故障记录在browser_source_failures，调度状态在source_loop_stores，解析记录在source_seed_resolutions，模块操作在pipeline_module_events。

2026-09-13：32项来源、分页、暂停、重试、末页新代次、录取轮转相关测试通过。真实轮转中店铺A导航失败后保留断点，店铺B完成3页，新增24个；随后工作进程自动继续其他店铺。实时计数见output/playwright/source-loop-acceptance.json。

当前仍不能承诺无限增加新店铺。新商品回流到原来源店铺属于补充根证据；只有新种子解析出未见过的真实店铺ID才是来源增长。直连接口不返回店铺ID的种子仍待补齐。采集成功、进入测算、提交接受与确认上架分别统计。

20:47收尾复核：46项相关测试通过。Playwright累计新增1064个候选，7个已被下游领取：2个补资料、3个测算拒绝、2个上架提交已接受，均未计为确认可售。一间店铺已到明确末页；7个种子尚缺直连返回的店铺ID。新工作进程已接管，配置enabled=true；来源积压达到阈值后会停止扩采，随测算消耗恢复。计数是该时间点快照，不是最终500批次结果。
