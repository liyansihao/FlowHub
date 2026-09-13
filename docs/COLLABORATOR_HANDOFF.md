# 协作者交接：当前 FlowHub

更新日期：2026-09-13（多代来源扩展更新）。main 是本轮模块化改造的协作基线，仍是开发预览版。当前本地生产实例在运行；公开仓库不包含该实例的数据、账户或浏览器会话。现有桌面安装包没有随本次源码推送重新打包。

## 现在到了哪里

在原 FlowHub 的用户、店铺、数据库和界面上，重做了后台来源与发布调度。不是另建了一套账户系统，也不是完整替换所有旧代码。

已经有真实证据证明：来源页面读取、分页续采、跨页去重、候选自动入队、毛子补资料、CompareBot测算和上架提交可以连通。部分样本已读到明确店铺末页；失败来源不会阻断其他店铺。这里的“提交接受”与“确认可售”分别记录，不能互换。

新增“商品SKU → 其他卖家弹层 → 新店铺一页采样 → 新商品 → 下一代探索种子”分支，商品不必先上架或产生订单才能探索。当前私有验收记录证明了从原种子发现新店铺，并由第二代商品发现更多店铺、产生第三代探索种子；早期8个样本中有3个确认上架、3个因利润不足淘汰、2个待审核。这是特定样本快照，不能据此推算整体通过率。

`exploration_only` 只是待筛选的探索资格，不能等同来源合格或允许上架。完整商业筛选和下架排除仍保留；新店铺需采样商品通过来源过滤才加入持续整店轮转。详见[其他卖家扩展](other-seller-expansion.md)。

尚未证明：任意店铺均能采完、无人值守长期稳定达到40件/小时、来源店铺无限增长，或克隆本仓库即可完整运行真实业务。单元测试和短时采集速度不能证明这些目标。

## 从哪里读代码

| 模块 | 入口 | 职责 |
| --- | --- | --- |
| 总调度 | `flowhub/worker.py`、`pipeline_modules/runtime.py` | 独立来源、补资料、审核、提交、回查通道 |
| 种子与整店来源 | `pipeline_modules/source_loop.py`、`browser_source.py`、`bridges/playwright-source.mjs` | 精确来源绑定、持久会话、轮转、暂停、断点、重试、复采 |
| 其他卖家与多代探索 | `other_sellers.py`、`bridges/other-sellers.mjs`、`discovery_facts.py` | 独立发现种子、真实卖家弹层、采样过滤、父级证据与generation；直连事实保留原统计周期 |
| 候选录取 | `pipeline_modules/admission.py` | 新旧候选交替、有限验收集合优先、SKU去重、队列上限、目标路由与授权范围 |
| 资料补齐 | `pipeline_modules/repair.py`、`source_detail.py` | 自有授权目录基础字段及毛子草稿资料；逐字段保留来源和时间 |
| 1688/DINO/千问/利润 | `plugin_comparebot.py`、`comparebot_process.py`、`vendor/compareBot` | 供应商匹配、常驻模型、审核、成本和利润证据 |
| 发布 | `plugin_pipeline.py`、`plugin_publication.py` | 持久发布意图、提交、低频回查、库存与可售确认 |
| 页面与接口 | `source_api.py`、`web/assets/app.js` | 沿用现有界面与用户权限；部分新控制仍通过CLI完成 |

表中未写完整目录的Python文件均在flowhub下。旧seed_pipeline、hour_acceptance等固定批次模块保留作历史参考，直接运行入口已停用；不要恢复历史硬编码授权。

## 适合先分工处理的缺口

1. **探索后的资料补齐与质量收敛。** 其他卖家入口已能带来新店铺和下一代商品，下一步重点是把探索种子补成来源合格商品，并记录各代新增、重复、缺项和通过率。毛子sku3未声明的统计周期不能写成28天，未返回的跟卖许可保持未知。部分当前卖家解析仍缺ID，不猜卖家、不用榜单冒充整店。
2. **持久运行。** 其他卖家弹层仅在已读唯一店铺数等于页面声明数时记complete；partial现在会重新加载并扩大滚动预算，尚无服务端游标续读。补齐会话失效恢复、分页结构变化的回归样本和连续运行观测。验证码/身份异常应停当前来源；网络错误有界退避；来源积压达到阈值后暂停扩采。
3. **可移植部署。** 真实测算/发布仍依赖仓库外的毛子、FlowEF入口、本地模型和配置。先把依赖逐个变成明确接口和安装约束，再谈跨电脑部署。
4. **可观测界面。** 将CLI已有的各通道状态、资料缺项、来源增量、提交接受、确认可售分开呈现。不要把历史报告数字做成实时进度。

## 新增配置和兼容点

- `source-loop.json`：`other_sellers_enabled`启用卖家发现；`discovery_interval_seconds`默认120秒；`explore_pending_sources`须显式开启才允许无明确筛选失败的待补齐商品探索。示例保持关闭，不代替真实联调授权。
- `source_discovery_seeds`与自有商品`sourcing_seeds`分开；新来源没有本店offer，不应人为补造。CompareBot的`eligible_roots`区分本店offer根和其他卖家页面根，后者核对seed SKU、页面hash及当前卖家归属，修复直接读取shop/offer的KeyError。
- campaign的`acceptance_skus`最多100个，仅改变录取优先级，仍受并发、总量、去重和下架排除限制。
- `python -m flowhub.pipeline_modules status`的sources增加`other_seller_discovery`，区分发现种子状态、观察到的店铺数和新店铺筛选状态。

## 开发验证

建议使用Python 3.12和Node 22，自己的clone/工作树和虚拟环境：

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]' -e ./vendor/compareBot
npm ci --ignore-scripts
.venv/bin/python -m pytest -q
npm test
```

正式Python测试包括tests和vendor/compareBot/tests；报告目录不参与发现。本轮多代来源扩展验证：333项Python测试通过；完整Node集成20项通过。干净检出复验不是全新操作系统安装验收。

`npm test` 是仓库内的15项Node测试，不发真实商品写请求；`npm run test:integration` 另包含依赖相邻ozon-runtime的5项策略测试。没有该外部依赖时先完成仓库内开发，不要编造通过结果。完整依赖与配置模板见[发布说明](PIPELINE_RELEASE.md)。

生产模型/接口联调还需CompareBot可选依赖、模型文件、自己的API凭证和明确授权的测试范围。默认关闭的deploy/examples配置不包含账号，不会授权发布。不要复制正在运行实例的data目录到开发工作树。

## 提交和验收约定

从main开个人功能分支，通过PR合并。描述具体触发条件、改后行为、如何验证、还有什么限制；按模块拆PR，避免把采集、利润规则和发布授权同时改动。优先给身份绑定、去重、租约、暂停/恢复和未知写入增加针对性测试。

不能为了提速跳过明确下架清单、利润审核、用户隔离或重复发布保护；已提交但结果未知时先对账，不盲目重发。公开Issue、PR和fixtures只用合成数据，真实店铺/商品明细、凭证、截图、HTML、日志、数据库和浏览器profile保留在私有环境。

真实联调结果使用四个独立口径：新增不重复候选、测算通过、提交接受、确认可售。当前运行状态看本地持久审计；公开文档只描述能力、限制与测试基线。
