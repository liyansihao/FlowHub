# FlowHub 固定源码基线（2026-09-17）

版本标签：`runtime-20260917.1`。本次固定对象为主 FlowHub 工作目录的源码与配套外部源码；不是一次新的线上部署。

## 边界

- 主协调服务的 LaunchAgent 工作目录为 `/Users/mac/Desktop/ozon/FlowHub`。Personal 安装目录和 CompareBot 工作树仍有进程使用，不能作为旧目录删除。
- 426 个文件从现有工作目录捕获，包含此前未提交的模块、审核站点、Windows agent、测试和文档；机器部署元数据、凭据工具本机配置、数据库、密钥、浏览器资料未纳入。
- 原样源码已保存在主目录 `reports/runtime-baseline-20260917/flowhub-running-source-before-fixes.tar.gz`。本版本在该快照上补充版本资料，并修复审核表未初始化时补资料重试耗尽的异常：无证据时维持人工审核，不进入发布。
- 配套 566 个外部源码文件单独保存，并由 `deploy/runtime-external-source-20260917.json` 记录哈希。外部副本排除了运行数据、一般 JSON 配置、模型和虚拟环境，不能当成开箱即用部署包。
- 已安装 Python 包版本在 `deploy/runtime-python-packages-20260917.json`；安装依赖仍需结合现有 requirements 和 Node 锁文件。模型、浏览器与完整传递依赖尚未锁定。
- 没有重启、暂停、重新部署或迁移现有服务。进程可能保留启动时的模块；源码标签不证明所有进程已加载该版本，也不证明云端审核站点与标签一致。

## 校验

```sh
python3 scripts/check_fixed_release.py
python3 scripts/check_pipeline_dependencies.py
```

第一条核对固定的 FlowHub 文件与配套外部源码，返回非零说明缺失或漂移。可通过 `--legacy-root` 指向外部副本的父目录。清单核对已有文件，不负责发现新增文件；同时检查 `git status --short`。

## 验证

隔离副本中 Python 全量回归 713 项通过；主桥、Netlify、Vercel、Cloudflare Node 单元测试共 44 项通过。结果与源码归档保存在主目录 `reports/runtime-baseline-20260917/`。未进行真实商品发布验证。

## 后续开发与切换

从此标签创建新的 `codex/<topic>` 工作树开发；主运行目录保留给经验证的版本。禁止在运行目录直接做实验或清理数据。官方 API 分支的历史提交尚未整体合并，本次仅捕获主目录已有文件，不额外引入该分支差异。

若要切换全部运行进程，需先按 `PIPELINE_RELEASE.md` 排空在途写入、核对未知结果，再由现有 supervisor 重载并核验心跳、入口和代码版本；本次没有执行该切换。回退代码不能覆盖现有数据库或重放发布意图。
