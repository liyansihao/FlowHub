# 独立店铺采集脚本

入口：bridges/storefront-auto.mjs。它自行运行至明确末页、时间上限、人工暂停或不可重试错误；不依赖 Codex 逐页导航、保存文件或触发导入。

## 已验证的访问方式

本机已有 OpenCLI 1.8.6 与已连接的 1.0.24 浏览器桥。backend=opencli 使用独立命名会话/标签页，通过日常 Chrome 正常导航，再读取该页 DOM HTML。没有读取、导出、复制 Cookie，也没有安装新的扩展。需要用户保持 Chrome 与原有扩展可用。

新建 Playwright Chrome 配置预检返回 403，同一已有会话直接 fetch 返回无商品状态的页面，因此当前实际测试使用“正常导航 + DOM 读取”。独立脚本不等于不依赖浏览器；它不需要逐页人工操作，但仍依赖已连接的 Chrome。

## 命令

在 /Users/mac/Desktop/ozon 执行：

    node FlowHub/bridges/storefront-auto.mjs --backend opencli --data FlowHub/reports/seller-auto-20260912/data --output output/playwright/storefront-auto-ten-minute-20260912 --seller 1168944 --seconds 600 --interval 2000 --resume

仅验证访问，不开启计时采集：

    node FlowHub/bridges/storefront-auto.mjs --backend opencli --data FlowHub/reports/seller-auto-20260912/data --output output/playwright/storefront-auto-preflight --seller 1168944 --preflight

暂停：

    FlowHub/.venv/bin/python FlowHub/scripts/collect-storefront.py --data FlowHub/reports/seller-auto-20260912/data --owner independent-storefront pause 1168944

失败阻塞后，先看 events.jsonl、preflight.json、SQLite 审计，再明确 retry。普通恢复不会重置失败断点：

    FlowHub/.venv/bin/python FlowHub/scripts/collect-storefront.py --data FlowHub/reports/seller-auto-20260912/data --owner independent-storefront retry 1168944

## 校验与输出

- 启动先做真实商品页预检。预检失败时不开始十分钟测试。
- 每页验证店铺 ID、请求游标、实际页码和下一页地址；只取店铺 tileGridDesktop 商品区。
- 商品/审计/游标同一事务提交。重复 SKU 不重复新增。
- 加载期间外部暂停或达到时间上限，保留原页，不提交迟到结果。
- 临时旧页/加载错误最多尝试 3 次；验证码或非匹配来源立即停止。
- 已有 ERP 数据不会被不完整的浏览器数据覆盖。价格保留原币种，不默认换汇。
- events.jsonl 是独立运行事件；每页 HTML 单独保存；summary.json 是该次计时结果；export.json 含商品和事务审计。
- 输出目录 runner.lock 防止该目录并发运行；异常强杀后需核实进程已经退出再处理残留锁。
- SIGINT/SIGTERM 停止继续派发；下一次从数据库断点恢复。不要删除数据库来重启。

测试：tests/storefront-auto.test.mjs 覆盖真实游标行进、旧页重试、403 停止、外部暂停、时间上限、重试次数上限和导航边界；tests/test_storefront.py 验证解析与入库。实际历史页面 1–3 离线回放验证 22 个唯一 SKU 及第 4 页暂停断点。

限制：十分钟不等于全店完成；未读到明确末页只能报告部分覆盖。若浏览器桥断开、访问资格失效、页面结构改变，需要按审计处理，不能把无商品页面当作店铺为空。


## 本地调用超时恢复

第一轮实测在第 97 页出现本地调用超时，565 秒时安全停止，96 页/768 个 SKU 已入库；失败原始记录保留在 storefront-auto-ten-minute-20260912。

修复后的版本保留本地失败阶段与错误码，临时本地调用超时自动重试。如果调用已经提交但应答丢失，先查事务审计恢复成功结果，不再重复提交，也不会漏计已提交页。到达总时限时取消尚未完成的请求并暂停，不将正常截止误判为阻塞。对应新增 3 项故障注入回归测试，自动流程总计 10 项。

DOM 读取使用浏览器 Navigation Timing 的 responseStatus（可用时）记录状态，未知 HTTP 状态不伪装成 200；是否有效商品页始终由内容、身份和游标校验决定。
