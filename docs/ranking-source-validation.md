# 榜单优先方案：修改与验收记录

2026-10-07。当前状态：隔离代码修改和测试完成，**未部署、未启用生产**。

开发目录：`/Users/mac/Desktop/ozon/FlowHub-ranking-seed-priority`。
分支：`codex/ranking-seed-priority`。
起点：唯一生产提交 `e555cbbea43a066a5bd2386d00c40cc5c1b59400`。
桌面工作树工具绑定的是父仓库 ozon，无法解析 FlowHub 提交；工具返回 invalid reference 后，使用 FlowHub 自己的 Git 创建隔离工作树。未修改生产源码。

## 已实现

有近期销量的榜单商品优先，扩展商品补位；约每 6 小时回查榜首，分页进度持久保存，后续新准入自动回到榜单优先。新增种子需要自身销量，历史种子、已采候选和在途任务保留。旧 SKU 更新证据，不重复准入；榜单均价不冒充挂牌价。

必要接入仅覆盖来源采集与资格、准入顺序、审核/跟卖反馈的来源凭证识别，以及新凭证在既有收藏归档中的保存。同款、利润、价格、500g、库存、店铺轮转、发布后端、Offer 和未知写入规则未改，无附带重构。具体边界、配置及受控切换说明见 [方案说明](ranking-source-policy.md)。

## 测试结果

- 最终 Python 全量：**1,131 passed**，97.76 秒。两项为原依赖的弃用提示，无失败。
- JavaScript 集成：**27 passed**，无失败。
- 其中新增榜单专项：**25 passed**；另新增一项榜单来源贯穿原跟卖导入、库存回查的回归。
- 新文件 Ruff、桥接脚本语法检查、`git diff --check` 通过。

覆盖榜单优先与扩展补位、榜单更新后重新优先、断网/空榜/重复页/类目不匹配、跨重启续传、销量零/未知/过期、卖家绑定、历史种子保留、新成功上架商品不直接成为种子、探索开关不能绕过销量、人工禁止和已处理 SKU 不重入、刷新不改变原价格/审核/队列、积压不阻断榜单刷新、原跟卖及收藏归档保护。

所有接口写入测试都使用临时数据库与模拟响应，没有为测试发布真实商品。

## 原生产流程检查

只读基线核验 `ok=true`：源码指纹一致，仅一个主发布 worker，退休发布器均未恢复。

2026-10-07 22:41:23（北京时间）的过去 30 分钟窗口中，原流程记录了 24 条 publication/selling 事件，最后一条 publication 事件为 selling；同时存在审核推进、等待锁和人工审核等事件。说明原上架仍在推进，不是无故障或长期稳定认证；这些是上架状态事件，不是成交销量，也不是新方案上线效果。

验证原始日志：

- `/Users/mac/Desktop/ozon/output/lili-sales-analysis-20261007/ranking-change/pytest-final.log`
- `/Users/mac/Desktop/ozon/output/lili-sales-analysis-20261007/ranking-change/node-tests.log`
- `/Users/mac/Desktop/ozon/output/lili-sales-analysis-20261007/ranking-change/ranking-tests.log`
- `/Users/mac/Desktop/ozon/output/lili-sales-analysis-20261007/ranking-change/production-baseline-check.json`
- `/Users/mac/Desktop/ozon/output/lili-sales-analysis-20261007/ranking-change/production-progress.json`

本次请求完成到代码与回归验证；没有合并 main、推送、切换服务或写入生产策略开关。生产应用本方案仍需按唯一基线要求记录受控部署及上线验证。
