# 多电脑运行版本证据

## 上报机制

主业务 worker、协调 API 和网页 API 在启动时捕获源码指纹、Git 提交与工作树状态、Python 版本、安装包集合指纹及锁文件指纹。Mac 同时记录明确列出的外部源码目录指纹，本地 CompareBot 子进程记录自己的 Python 环境和启动源码，并在模型初始化后补充模型名称/revision。

这些是启动时观察到的磁盘文件，不是所有已加载机器码的远程证明。Git 提交号可为空，脏目录也会明确显示；实际源码哈希仍可用于区分两份安装。外部目录指纹覆盖当前 helper 明确列出的目录，不代表完整外部传递依赖。

Windows 更新后，ERP、Full Agent、计算子进程分别拥有独立 instance_id。每 30 秒通过独立 `/v1/runtime` 接口上报；不改变旧 ERP/compute 协议，不更新设备可执行任务的心跳或能力，也不派发任何任务。旧服务返回 404 时版本上报失败不阻断业务。计算端模型 revision 来自已成功加载的 ranker；本功能不验证模型权重文件哈希。

服务端使用已有设备凭据绑定报告归属，禁用设备不能报告；同进程 instance_id 的启动快照不可被修改。每个设备/组件最多保留 8 个近期进程报告。出现多个新鲜进程时清单会显示异常，不把它们合并成一个版本。

## 读取

```sh
python scripts/runtime_inventory.py --data /absolute/path/to/FlowHub/data
```

脚本使用 SQLite 只读连接，不初始化数据库、不连接店铺。结果包含本地进程启动快照、数据库实际 schema 指纹、各设备报告和新鲜程度。设备超过 90 秒未报告或缺少组件，显示 `missing_stale_or_multiple`。本地 `pid_exists_not_identity_proof` 只是进程号存在检查；文件的 observed_at 与进程启动时间还应交叉核对，避免 PID 复用误判。

协调服务 `/healthz` 保留原协议字段，追加本进程 runtime。服务启动后替换源码不会让旧进程健康接口自动变成新版。

## 部署和 Windows 更新

主控更新前按既有维护约定暂停新增通道、排空有效租约，检查 ERP/计算在途命令，并获取发布/采集/清理锁。保留原数据库、发布 intent、未知结果与设备注册。受 supervisor/LaunchAgent 管理的进程受控重启后，核对新 PID、源码指纹、原暂停状态恢复和 Windows 心跳。

Windows 小型更新包使用 `scripts/build_version_update.py` 构建。操作见 `RUNTIME_VERSIONS_WINDOWS.md`。它保留模型、环境、凭据和账本，但必须先排空并正常停止原执行端；没有远程升级能力的旧客户端必须在电脑上执行一次入口。

Personal 54fbf19 是独立安装。本次主控更新不会覆盖它；它原有健康接口继续报告其 Personal revision，不把它伪装成多电脑新版本。

## 验证

2026-09-17：103 项针对性测试通过，涵盖身份报告绑定与不可变性、未知/过期/多进程状态、旧协议兼容、更新锁、基线校验、失败回滚、原凭据与任务账本保留，以及已有上架响应丢失恢复。Windows 系统锁和启动入口仍需实机验收。
