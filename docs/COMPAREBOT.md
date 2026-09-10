# compareBot 1688 匹配接入

分支 `codex/comparebot-1688` 基于 FlowHub 已提交版本 `581421f`，未包含原工作目录的未提交修改。未连接或切换生产数据库、未启动生产工作流。

附件原样保存在 `vendor/compareBot`。FlowHub 保留 `flowb-matcher` 模块 ID，数据库初始化时把模块目录的旧 FlowB matcher 切为 compareBot；已有任务中冻结的模块快照不改写。示例流程仍使用 demo。仅管理员可使用此本机 ERP 兼容模块。

流程：compareBot CLI 图搜 / DINOv2 → 60% 同款判断 → 毛子直连商品政策、人工反馈、FBS、重量尺寸及邮政利润核验 → 原有利润、禁止清单和上架流程。ERP 桥接代码保存在 `bridges/comparebot.mjs`，不调用旧的 `verify1688` 分支。不需要 Chrome。

自 2026-09-11 起按用户要求，全部商品只走 DINOv2。使用附件的纯排序 CLI `comparebot.interfaces.cli`，选择最高分候选，相似度 **≥0.60（含边界）** 判为 `approved`，低于阈值或无候选判为 `rejected`。不调用千问，不按商品大小分流。附件源码保留原样，FlowHub 适配层覆盖其原来的分流策略。

同款通过后仍独立核验 ERP 来源、采购价、利润、禁止清单及重复上架。缺采购价进入人工审核，不估价。DINOv2 分数保留原值，结构分数为空；旧图片 / 结构阈值只用于旧 matcher。`origin.size`、附图和规格可保留为元数据，均不改变这个同款阈值。该规则的通过率不等于人工核验准确率。

## 安装与运行

在独立 Python 3.12 环境安装：

```sh
python3.12 -m venv .venv-comparebot
.venv-comparebot/bin/python -m pip install './vendor/compareBot[search1688,dinov2]' 'python-dotenv>=1.2,<2'
export FLOWHUB_COMPAREBOT_PYTHON="$PWD/.venv-comparebot/bin/python"
# 可选：cpu / mps / cuda
export FLOWHUB_COMPAREBOT_DEVICE=cpu
```

首次加载 DINOv2 会下载模型，建议先按附件 README 预热。单次 CLI 限时 140 秒，后续 ERP 限时 120 秒，总限时 280 秒，小于现有 300 秒任务租约。超时或取消会杀死并回收子进程。部署时需一起分发 vendor 和 bridges 目录；现有 Windows 安装包未重建。

工作流密钥继续支持旧 ERP Token 或 `{"erp_token":"YOUR_ERP_TOKEN"}` JSON。已保存 JSON 中的千问密钥不再使用，也不会传给模型子进程；屏蔽环境中的千问密钥和 dotenv 自动加载。利润模块仍使用原 ERP 密钥。

隔离试用部署地址 `http://127.0.0.1:38428`，图片对照页 `/trial/`。原 FlowHub 生产服务未切换，试用工作流保持暂停，避免自动批量发布。

## 本次验证

完整测试 126 项通过，匹配专项 38 项通过。匹配专项包含阈值边界、不同大小、无候选、异常分数、旧低分通过记录拦截，以及纯 DINO CLI 不接收千问凭据的测试。20 件实测样本按已有 DINOv2 原始分数重新判定，19 件同款、1 件不同款。未重新调用千问。历史对照见 `COMPAREBOT_TRIAL_20260911.md`，其中旧分流统计已被本规则取代。
