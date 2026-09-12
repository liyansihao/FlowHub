# 来源店铺：分页、暂停与恢复

现已增加 **Safari 真实店铺页面分页导入**，三店六页已验收，48 个新发现 SKU；这是浏览器辅助采集，需要逐页提供 Safari 保存的 HTML，并非无人值守浏览器后台任务。店铺全量是否读完以显式末页为准。另保留毛子 ERP 榜单分页后精确匹配来源卖家通道，榜单本身**不是整店覆盖**。原生 HTTP 已修复重定向 Cookie 回传；当前真实三店请求在普通会话处理后返回 Ozon 403 验证码要求。`native-shop.mjs` 使用内存 Cookie、同源/同店重定向校验、最多 3 次重定向及 15 秒总超时。验证码响应立即停止；合法 JSON 可返回供后续验证，不把 HTTP 200 当成已验证整店采集。详见 [第二轮修复证据](../reports/seller-native-cookie-20260912/修复进展.md)。

本地 macOS/Linux 命令，从 `/Users/mac/Desktop/ozon` 执行：

```sh
FlowHub/.venv/bin/python FlowHub/scripts/collect-source-sellers.py status --output FlowHub/reports/seller-native-acceptance-20260912
FlowHub/.venv/bin/python FlowHub/scripts/collect-source-sellers.py resume --output FlowHub/reports/seller-native-acceptance-20260912 --sellers 1168944,2549888,4740958
FlowHub/.venv/bin/python FlowHub/scripts/collect-source-sellers.py run --output FlowHub/reports/seller-native-acceptance-20260912 --pages 6 --seconds 120
```

`run` 不隐式恢复任务，预算结束后自动暂停选定店铺的流。`--seconds` 是停止派发新页的时间上限，已发出的单页请求允许安全结束（bridge 总等待上限 100 秒）；暂停命令也采用这个语义。强制杀死进程后，未提交页会在最长 150 秒租约过期后重新领取，已提交页不回退。`resume` 不清除末页的下次刷新时间；真正阻塞的任务需明确 `retry`。

```sh
FlowHub/.venv/bin/python FlowHub/scripts/collect-source-sellers.py pause --output FlowHub/reports/seller-native-acceptance-20260912
FlowHub/.venv/bin/python FlowHub/scripts/collect-source-sellers.py retry --output FlowHub/reports/seller-native-acceptance-20260912
FlowHub/.venv/bin/python FlowHub/scripts/collect-source-sellers.py probe-native --output FlowHub/reports/seller-native-acceptance-20260912
```

首次运行或根种子超过 6 小时需 `prepare`：从原始证据恢复 153 家清单，导入当前本地上架历史，并实时核对选定店铺的根种子与下架清单。它会暂停该验收库的所有任务，保留已有游标和商品数据。新店铺批次通过 `--sellers` 明确选择；准备失败的来源会保留在 `seed-failures.jsonl`，其种子不能驱动扫描。不要删掉数据库来“恢复”。

```sh
FlowHub/.venv/bin/python FlowHub/scripts/collect-source-sellers.py prepare --output FlowHub/reports/seller-native-acceptance-20260912 --sellers 1168944,2549888,4740958
```

认证复用 `ozon-runtime/lib/maozi-credentials.mjs`，请求复用全局限速直连 transport，强制 native backend。目录内 SQLite 是游标/数据/审计的权威状态；JSON 是可重新导出的快照。`candidates.json` 是本库不重复且排除已上架历史的当前初筛结果，`new-candidates.json` 还排除了前次证据的候选 SKU。后者可以为空，不能把复查旧商品计为新增。

来源 API（重新启动使用这份源码的 API 服务后生效）：

- `GET /api/sources/tasks?offset=0`：每次最多 100 条任务，含页码、状态、最后错误。
- `POST /api/sources/tasks/{task_id}/pause`：暂停，不丢弃正在返回的页。
- `POST /api/sources/tasks/{task_id}/resume`：恢复暂停任务，保留游标及调度时间。
- `POST /api/sources/tasks/{task_id}/retry`：重试阻塞任务，仍从失败页继续。
- `GET /api/sources/tasks/{task_id}/attempts?cursor=0`：按审计 ID 分页，最多 50 条，保留每次来源、响应和失败原因。

这些 API 使用既有登录/CSRF/工作区权限机制。它们不启动上架工作流。原生探测失败的完整证据保存在 CLI 请求日志；不能将诊断成功理解为整店读取成功。


## Safari 店铺分页

FlowHub「商品来源与筛选」中新增「店铺分页采集 · Safari」：选择店铺，点击继续，打开当前分页地址，在 Safari 中用文件→存储为→**页面源码**保存 HTML，选择该文件并导入。页面地址从已验证响应提取，不手工拼接 page=数字。界面会更新到下一页；暂停后不能导入新页，恢复仍使用同一游标；失败需重试，错误和失败页均保留。

当前本地源码服务（38427）的 admin 工作区已导入 153 店任务与 48 个待核查发现商品。全部店铺暂停，三店停在第 3 页，其余 150 店仍在第 1 页。

命令行使用 scripts/collect-storefront.py，必填 --data（数据库目录），可选 --owner（默认 seller-acceptance）。子命令为 status、export、prepare --evidence 原始证据目录、resume 店铺ID、pause 店铺ID、retry 店铺ID、import 店铺ID HTML绝对路径。import 默认读取持久化游标，也可用 --url 明确传入实际请求地址。验收库目录为 reports/seller-browser-20260912/data。

storefront_tasks 持久化每店真实 next_url；页面观测时间取响应 current.time，导入时间独立保存。页面超过六小时、验证码、店铺或游标不符、缺失分页字段、重复商品页都会保留失败原因并停止该店。重复导入相同 HTML 为幂等重放，新增为 0。商品与游标、审计在同一 SQLite 事务提交。只解析 tileGridDesktop，排除推荐 skuGrid；不使用 eval，不读取或搬运浏览器 Cookie。

新增 API 受既有登录、工作区权限与 CSRF 保护：

- GET /api/sources/storefront：店铺任务列表
- POST /api/sources/storefront/prepare：从当前工作区已有来源种子建立任务
- POST /api/sources/storefront/{seller}/pause、resume、retry：控制单店任务
- POST /api/sources/storefront/{seller}/import：JSON 包含 html、requested_url
- GET /api/sources/storefront/{seller}/attempts?cursor=0：分页读取审计

店铺当前价存为 current_price_rub，不充当 ERP 统计均价；物流、重量、销量、可跟卖未知时不默认合格。发现商品可继续使用既有毛子 ERP 直连接口补齐资料。重复 SKU 在发现候选导出中合并，历史商品和下架 block 排除，正式筛选仍走既有来源库规则。验收目录另提供排除上一轮 65 个候选后的 new-candidates.json。


## Chrome 实测补充（2026-09-12）

Chrome 原生窗口的“另存为 → 网页，仅 HTML”已通过一个店铺两页实测：16 个不同 SKU，暂停/恢复成功，重放新增为 0。Chrome 标签页扩展读取接口仍超时，因此不能宣称无人值守 Chrome 采集已完成。

此次 Chrome 会话显示人民币符号 ¥，之前的只认卢布解析导致 schema:ValueError。已修复为保留 current_price_display，只有明确带 ₽ 的价格填入 current_price_rub，避免错误币种参与计算。解析器会继续保留来源币种文本，不做猜测换汇。新增币种回归测试，店铺模块 9 项测试通过。Chrome 实测证据位于 reports/seller-chrome-20260912/acceptance.json。
