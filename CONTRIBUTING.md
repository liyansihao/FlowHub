# 协作开发

当前发布基线为 [`runtime-20260917.1`](docs/FIXED_RUNTIME_20260917.md)，源码提交 `fbf2487`。`main` 可继续增加文档和经过评审的改动；复现固定版时使用标签。先读[协作者交接](docs/COLLABORATOR_HANDOFF.md)，再选择模块与验收目标。

## 建立独立开发目录

```sh
git clone https://github.com/liyansihao/FlowHub.git
cd FlowHub
git switch -c codex/my-change origin/main
```

如果已有仓库，使用独立 worktree：

```sh
git fetch origin --tags
git worktree add -b codex/my-change ../FlowHub-my-change origin/main
```

不要在正在运行服务的目录切分支、改代码或运行迁移。开发环境使用自己的临时数据目录，不复制生产 `data`、密钥、浏览器 profile、配置或账号。不同开发者分别使用自己的分支、虚拟环境和测试数据。

## 依赖和验证

建议 Python 3.12、Node 22。在独立开发目录安装：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' -e ./vendor/compareBot
npm ci --ignore-scripts
npm test
```

`npm test` 是仓库内的桥接测试。完整 Python 测试会导入相邻 `FlowEF-production/src`；完整 Node 策略测试会导入相邻 `ozon-runtime`。这些代码不包含在本 Git 仓库，缺少时不能宣称全量测试通过。向维护者索取经过审核的源码依赖包，并按如下结构放置：

```text
workspace/
  FlowHub/
  FlowEF-production/src/flowef/
  ozon-runtime/lib/
  maozi_direct_new_method/
  flow_ef_category_fbs/
  flow_b_ef/
  flow_b_codex_transfer_20260608/
```

环境变量不能替代所有相对路径：部分 Python 模块和 Node 测试直接引用相邻目录。配套源码哈希见 `deploy/runtime-external-source-20260917.json`；运行数据、模型和账号配置不随源码分发。依赖到位后：

```sh
.venv/bin/python -m pytest -q
npm run test:integration
npm --prefix netlify-review ci --ignore-scripts
node --test tests/*.test.mjs netlify-review/tests/*.test.mjs vercel-review/tests/*.test.mjs cloudflare-review/tests/store.test.mjs
```

固定版在隔离副本中通过 713 项 Python、44 项 Node 测试。此结果不代表新电脑安装、云端部署或真实商品写入已验收。数据库集成脚本、浏览器集成脚本和部署命令不属于上述单元测试。

固定版校验应在标签的独立工作树执行：

```sh
git worktree add --detach ../FlowHub-baseline runtime-20260917.1
cd ../FlowHub-baseline
python3 scripts/check_fixed_release.py --legacy-root /path/to/verified-dependencies
```

哈希清单描述标签当时的文件；后续文档或代码变更也会被报告为差异，不应为掩盖差异而随意更新清单。该工具不检查新增文件，同时查看 `git status --short`。

## 分工与 Pull Request

先用 Issue 或 PR 说明问题、负责模块和验收结果。模块入口见交接文档。一个 PR 围绕一个可验证的问题；涉及接口、配置或数据库结构时同步更新消费者、兼容说明和测试。

PR 写清楚触发条件、改前/改后行为、验证命令与结果、外部依赖和剩余限制。重点覆盖身份绑定、去重、租约、暂停恢复、未知写入回查与用户隔离。使用仓库的 PR 模板，不把测试通过写成真实上架成功。

合并到 `main` 与部署分别进行。发布负责人验证提交后创建新标签，按[发布与回滚说明](docs/PIPELINE_RELEASE.md)处理在途任务、服务重载及现场验收。固定标签不移动，不在后台仍执行写操作时直接回退数据库或重放队列。

## 数据与操作边界

禁止提交 `.env`、密钥、会话、数据库、账号导出、浏览器配置、真实商品清单、原始采集页面及运行日志。Issue、PR 和测试样例只使用合成或审核后的脱敏数据。发现敏感信息不要复制到公开讨论。

真实接口、采集、发布、库存和删除操作需使用明确授权的账号与范围。保留来源、时间、失败原因及回查证据；明确下架清单优先。结果未知时先对账，不盲目重发、补库存或重新上架。
