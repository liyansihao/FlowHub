# Playwright 店铺列表接入

仅迁移原项目的有界面 Chromium 持久化会话和页面加载方式。保留用户安装的插件；列表采集器不调用插件，也不读取ERP令牌。下游仍用现有毛子直连和CompareBot。

- `bridges/playwright-source.mjs`：指定持久化 profile，顺序导航当前真实游标，等待页面状态并保存浏览器DOM HTML。
- `flowhub/browser_source.py`：按owner/run/seller保存独立扫描游标，不重置旧整店记录；解析现有Ozon页面状态，仅接收绑定店铺的商品网格。新增SKU与游标在同一事务提交。
- 浏览器任务暂停后不再提交新页面，继续运行时沿已保存的next_url恢复；重复文件不重复入库；旧SKU保留已有详细资料。
- 403/验证码、导航失败或页面身份不匹配停止当前来源，保留失败原因及页码；只有retry才恢复。

运行前用 CLI `python -m flowhub.browser_source next --owner OWNER --run-id RUN --seller SELLER` 检查状态。pause/resume/retry 使用相同参数。运行传输：

```sh
FLOWHUB_SOURCE_PROFILE=/path/to/dedicated/profile node bridges/playwright-source.mjs OWNER RUN SELLER 3
```

profile不能同时由CLI浏览器和采集器占用。不要使用个人默认Chrome配置目录。

2026-09-13首次尝试：新建profile与原项目 专用持久化 profile 均触发403。用户随后在原项目持久化浏览器中恢复正常访问并安装插件。

2026-09-13 20:23复核：通过上述原项目profile真实读取店铺A的10页（80个不同SKU，新增7个），以及店铺B的3页（24个不同SKU，新增24个）。总计104个不同SKU，全部可在FlowHub来源库查询，其中31个本次新增。第一家采集3页后暂停并关闭进程，恢复后从第4页继续到第10页；目前断点分别为第11页和第4页。重放已入库真实HTML返回replay、added=0，游标不变。

本次修复了初始200后再次导航导致response正文丢失，以及店铺网址只有名称、不带数字ID的兼容问题。后者仍强制校验页面analyticsInfo.sellerId及后续路径归属。之前的挑战页和身份格式失败记录保留，重试后通过。

25项相关测试通过，Node语法检查和git diff --check通过。验收记录为 `output/playwright/playwright-source-20260913/validation.json`，页面HTML及截图在同目录店铺子目录。153个来源已初始化，本次仅对2家做小批真实验收，其他来源保持暂停；尚未证明整店末页完成、长期无人值守稳定性或这31个候选的测算上架结果。每次有界采集完成会关闭其专用浏览器，持久化会话保留。

后续同日已接入常驻工作进程的自动来源轮转，参见 [持续来源闭环](source-loop.md)。上述151家暂停是小批验收时的状态；启用闭环后153家均已纳入调度。当前状态以source-loop.json和source_loop_stores为准。
