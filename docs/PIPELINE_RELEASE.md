# 模块化流水线发布与回滚

本版将种子来源、毛子补资料、CompareBot 审核、发布提交和回查作为独立工作通道。保留原账户、店铺与数据库，新增的表用于队列、来源断点及审计。页面功能复用原 FlowHub；不需要新建账号或重新导入现有运行数据。

## 版本包含与依赖

代码提交包含 flowhub/pipeline_modules、Playwright 来源桥、CompareBot 常驻进程、发布队列及相应测试。历史固定批次模块保留供审计和单元测试，直接运行入口已停用，不能用历史批次授权启动新发布。

凭证、数据库、浏览器 profile、报告及 .playwright-cli 不进入 Git。配置模板见 deploy/examples，默认关闭；已有生产 data 下的配置不会被模板覆盖。候选录取、目标店铺、额度、500个上限和写入权限仍保存在当前数据库中，模板本身不授权上架。

运行依赖：Python 3.12+、项目 requirements.lock.txt；CompareBot 的独立环境及本地模型（FLOWHUB_COMPAREBOT_PYTHON可指定）；Node 与本地 Playwright 包、已授权的专用浏览器profile；相邻工作区毛子和 FlowEF 运行库。FLOWHUB_LEGACY_ROOT指定毛子工作区，部分兼容桥使用FLOWEF_LEGACY_ROOT，应指向同一目录。

`python scripts/check_pipeline_dependencies.py` 只读核验已测试的外部入口文件哈希。它不是全部传递依赖锁，也不会安装软件或读取凭证。FlowHub main 的提交不包含相邻仓库的代码、模型和账户配置，因此仅克隆此仓库不能宣称已复现生产环境。

## 验证与启用

```sh
.venv/bin/python -m pytest -q
node --test tests/*.test.mjs
python scripts/check_pipeline_dependencies.py
.venv/bin/python -m flowhub.pipeline_modules status
```

新环境先填写并审核配置模板，连接本地加密账户、检查仓库和当前明确下架清单，恢复专用profile的正常访问，再启用对应配置。生产写入范围由已有pipeline_campaigns和routes决定。已有本机部署合并main时保留原data目录与配置；通过`scripts/reload_pipeline.py`在暂停并排空在途操作后重启工作进程，由已有supervisor拉起。

## 回滚

合并前记录的代码基线为d4a565a，原本地验收提交仅保留在本地开发分支，不作为公开版本历史上传。合并前私有SQLite备份和配置保存在reports/main-merge-20260913，未加入Git。

先暂停seed、review、publication，并排空在途操作，再停止工作进程。将pipeline-v2.json和source-loop.json置为enabled:false；切回已验证代码版本后，应先核对持久发布意图，再决定恢复哪些工作。旧main不消费新plugin队列，不能假定回滚代码后新队列会自动继续。

保留数据库、去重记录、未知写入和平台任务ID，不重建队列，不重放已接受的提交。已发生真实发布后，不直接把旧备份覆盖到当前数据库；备份用于灾难恢复，恢复时必须先对账外部平台状态。

本次完整本地测试：323项Python、20项Node通过。测试覆盖软件行为，不代替长期吞吐验收。候选、测算通过、提交接受和确认可售分别统计。

干净检出验证：323项正式Python测试和20项Node测试通过；本地报告目录中的7项历史实验测试不计入发布验收，pytest固定发现tests和vendor/compareBot/tests目录。
