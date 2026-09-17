# 多电脑第一阶段：Mac中控 + Windows接入验收

已新增独立的loopback接入服务38428、设备凭证、一次性30分钟邀请码、中央SQLite验收任务队列、120秒租约、心跳延续、重复回传校验、设备撤销、Windows标准库客户端及登录自启动脚本。

数据独立保存在data/cluster，不开放现有生产SQLite和ERP密钥。服务安装为当前Mac用户LaunchAgent com.flowhub.coordinator，通过caffeinate -is运行；与现有生产worker分开，不需要停止正常上架。

跨地域连接选择Tailscale Serve的私有HTTPS入口。当前Mac未安装/登录Tailscale，因此只验证了本机HTTP协议和模拟多执行端，远端地址未建立。需要用户完成Mac和Windows的私有网络登录，然后领取一次性邀请码。

现阶段任务kind仅支持probe。没有开放生产发布、库存修改、任意命令或任意URL执行。生产适配下一阶段必须把现有本地SKU/store锁、journal、全局限速、所有权和下架守卫统一接入中央协调；不能把现在验收任务的去重等同于已经完成生产跨机去重。

验证覆盖见tests/test_cluster.py。本机实际启动两个独立agent进程，通过HTTP领取不同验收任务并回传；真实Windows及PowerShell未在当前环境执行，待第二台电脑到位验收。

交付包：output/FlowHub-Windows-Agent-Phase1.zip。详细步骤见packaging/windows-agent/README.md。
