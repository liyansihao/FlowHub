# compareBot 持续上架修复

`flowhub.continuous` 复用数据库 Worker 状态机，贯通补候选、匹配、利润、选店、俄文标题、ERP 发布、库存与可售回查。找到合格商品不再退出。发布与库存写入前保存不可重放标记；结果不明只回查同一 Offer。每个新商品库存固定1。原 Worker 的飞书下架检查、人工反馈和全局 SKU 占用检查继续执行。

`WarmScreening` 在专用 worker 进程内共享一个 DINOv2 模型和向量缓存，复用图搜结果做千问审核，不为每件商品启动模型子进程。业务 API 仍遵守共享 ERP 限速，未提高限流参数。同步店铺采用190秒间隔，限频错误不终止商品回查。ERP 成功阶段耗时修复为实际记录 comparebot_evaluate 请求。

当前部署使用已配置的丽丽一号、丽丽四号：前者额度不足可选后者；只能使用启用且核验通过的店铺。配置与凭据保存在原隔离试用数据库，不进入代码。

运行：

```sh
PYTHONPATH=/Users/mac/Desktop/ozon/FlowHub /Users/mac/Desktop/ozon/FlowHub-comparebot/.venv/bin/python -m flowhub.continuous --data /Users/mac/Desktop/ozon/FlowHub-comparebot/data/comparebot-trial/runtime
```

`scripts/continuous-supervisor.py` 是本工作区退出重启包装，已部署副本位于试用目录。它采用独占锁避免多监护进程；worker也使用独占锁避免多写入进程。后台独立于对话运行，异常退出30秒后重启。停止前创建试用目录 `continuous.stop`，再停止 worker 和 supervisor PID 文件对应进程。未知提交状态必须保留，恢复后只回查。

macOS LaunchAgent 的 Python 初始化在读取环境路径时阻塞，因此已卸载并保留 `.plist.disabled`，当前不声称重启电脑后自动启动。当前后台进程从 Codex 已验证的运行环境启动，日志为 `continuous.log`、`continuous.err.log`、`continuous-restarts.jsonl`。旧每10分钟接力任务ID已不存在；持续执行不依赖它。

验证：165项测试通过，含模型跨商品复用、Decimal结果可序列化、稳定Offer ID、同步失败后继续回查。真实启动验证 worker 心跳和候选游标持续推进。尚未用上线后的持续流统计足够样本，不承诺提速倍数。


## 吞吐修复

筛选和履约使用独立协程及分开的数据库领取条件：queued 只由筛选循环领取，其他非终态只由一个履约循环处理。事务租约防止同一任务重复领取；仍只有一个发布/库存写入执行通道。慢模型与平台回查不会相互等待，ERP全局限速保持原值。

店铺元数据读取缓存60秒，选店额度缓存30秒；发布前实时查额度，准备/发布/补库存前强制刷新店铺和仓库。缓存按凭据隔离；旧桥接环境凭据边界串行化，避免不同账号串用token。每次模块成功调用记录operation及耗时。

平台审核错误保存具体代码，错误商品隔离。目标仓库库存返回空记录时从30秒递增至300秒回查，不重放库存写入。2026-09-11实际确认1919921257已经selling；3447973522为DESCRIPTION_DECLINE（图片与类型不符），3180206074空库存记录仍待确认，不计成功。
