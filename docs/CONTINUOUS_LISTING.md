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

## 官方发布通道（2026-09-11）

用户确认后，具备官方凭据的店铺改用已有 OzonDirectPublisher：新任务 prepare 构建和冻结资料，publish 根据 frozen 标记选择官方提交；旧 ERP prepared 任务继续原协议提交。所有原 Offer 的商品状态与库存读写经官方接口执行，不迁移或重建商品。未知库存写入仍只回查；库存未达目标退避至300秒。发布前原有飞书、来源占用和实时利润保护继续生效。

原商品资料采集和利润仍依赖 ERP。官方路径使用原始俄文资料，跳过额外千问翻译；同款审核策略不变。两家店复用原账号核验的 store-vat.json。

失败调用增加 operation_error 耗时、异常类型及代码位置；原始错误仅加密保存在 continuous_errors，避免公开日志泄漏凭据。compat 保留加密诊断需要的上游错误，不再只剩笼统的 adapter unavailable。

首轮加密诊断定位到两次重复失败均为 `Local official commission or cost inputs unavailable`，现将该明确缺资料错误转为人工待补资料，跳过后续千问和自动重试；不把未知利润当成达标。其他网络/服务异常保留有上限的自动重试及加密诊断。

## 用户批准的佣金价格兜底

2026-09-11：生产本地计算器先查准确类目佣金；匹配失败时 realFBS 售价≤1500 RUB 使用12%，>1500 RUB使用24%。使用佣金估算时 commission.estimated=true，保存fallback_reason，calculation_source/commission_status明确标记估算，不伪造官方类目或俄文类型名。金额由实际CNY售价和汇率换算。重量、尺寸、品牌同款审核、物流线路和25%成本利润率门槛不变；当前邮政线路自身≤1500 RUB限制不因佣金兜底放宽。
