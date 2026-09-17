# 验证记录 · 1.0.0

验证日期：2026-09-13。

## 已完成

- Mac：通过双击入口对应的 Bash 脚本自动找到已安装 FlowHub Python 环境和数据目录。
- Chrome：读取本机普通用户配置中的毛子当前访问记录，包含 LevelDB SST、Snappy 解压和日志解析；未启动或控制浏览器。
- 真实接口：`https://api.maozierp.com/api.shop/lists` 返回成功；23 家 FlowHub 店铺的店铺 ID 与 Client ID 全部匹配。
- 只检查模式通过。更新模式也通过，报告当前凭证已为最新，0 家店铺 / 0 个模块需要再次写入。
- 自动测试 20 项通过：原生 Snappy 编码、损坏数据拒绝、记录序号/删除标记、UTF-16、跨块日志、过期凭证、只读检查、实际加密写入与备份、保留其他 API 密钥/开关/任务、重复执行、账号/店铺身份不匹配、并发修改回滚、不初始化缺失数据库。
- Python 静态检查、编译检查和 Mac Bash 语法检查通过。

## 待目标 Windows 验证

当前测试机器没有 Windows、PowerShell 或 Docker，未将静态检查冒充 Windows 实机测试。Windows 脚本依据现有 FlowHub Windows Docker 包布局编写，首次应运行 `Check-Windows.cmd`，确认 Docker 挂载、Chrome 文件读取与直连网络正常，再运行更新入口。

Windows 验收成功条件：检查模式显示 `verified: true`；更新模式显示完成；FlowHub 原有工作流开关、任务和其他密钥不变。若文件被 Chrome 锁定，先正常退出 Chrome 再重试。

本工具不保证 Chrome 或毛子未来版本更改存储协议后继续兼容；遇到不识别的记录会停止。
