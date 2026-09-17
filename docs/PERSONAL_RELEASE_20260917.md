# FlowHub 个人版保存点：personal-20260917.1

本版以 54fbf19 为基础，保存个人版店铺勾选、后台启动适配、连接导入及 Windows 打包等此前未提交的修改。它与多电脑版 multipc-20260917.1 分开保存，不合并 main，不改变现有运行服务。

## 与安装副本的核对

保存时，Mac Personal 安装目录的 84 个可对应源码文件与本版开发快照逐字节一致；另有 30 个 CompareBot build 副本。安装源码与这些构建副本另有私有本地归档。运行进程来自 Personal 安装目录，本版本没有启动身份上报，因此这里只确认磁盘源码对应关系，不声称逐模块验证进程内存。

当前安装服务使用独立端口 38437、独立 backend-data 与 legacy 依赖目录。源码路径和数据路径见原有 [Personal 安装说明](PERSONAL-54fbf19.md)。该说明中的任务数量和暂停状态是历史记录，不代表保存时实时业务状态。

## 本地备份

维护者工作区 FlowHub/reports/personal-20260917.1/ 包含：

- 本标签源码归档、Git 历史 bundle。
- installed-runtime-source.tar.gz：安装副本源码及构建副本（筛选源码扩展名）。
- external-source.tar.gz：已安装 legacy 中筛选的配套源码，不含浏览器、node_modules、模型及私有运行资料。
- installed-source-manifest.json：对应文件哈希；python-packages.json：Personal Python 包版本。
- private/：SQLite online backup、master.key、配置和 LaunchAgent，禁止公开。数据库完整性检查通过。
- 测试日志与 SHA256SUMS。

数据库备份保留账号及任务，但外部平台状态会继续变化；配置文件与数据库不保证同一事务时刻。恢复前必须停相关服务并对账，不能直接覆盖运行数据或重放历史商品提交。

## 验证与复现

在独立发布工作树使用 Python 3.12，完整测试需要 Personal 配套 legacy/FlowEF-production/src：

```sh
PYTHONPATH=/path/to/personal/legacy/FlowEF-production/src python -m pytest -q tests vendor/compareBot/tests
```

本次完整回归 115 项通过。首次测试缺少相邻 FlowEF 导入路径，补充 PYTHONPATH 后全量复测通过。测试使用主工作区 Python 3.12 测试环境与 Personal 安装的 legacy 源码。

此次没有修改业务逻辑，没有进行真实发布、库存写入或 Windows 部署。Windows 安装包和 Mac 应用并未重新构建；此版本是后端源码保存点，不能当作新的通用一键安装器。模型、浏览器及完整系统依赖也未封装。

后续修改从 personal-20260917.1 创建独立工作树，验证后创建新标签，不移动已有标签。个人版与多电脑版不得未经授权同时处理同一店铺的发布任务。
