# compareBot 1688 匹配接入

分支 `codex/comparebot-1688` 基于 FlowHub 已提交版本 `581421f`，未包含原工作目录的未提交修改。未连接或切换生产数据库、未启动生产工作流。

附件原样保存在 `vendor/compareBot`。FlowHub 保留 `flowb-matcher` 模块 ID，数据库初始化时把模块目录的旧 FlowB matcher 切为 compareBot；已有任务中冻结的模块快照不改写。示例流程仍使用 demo。仅管理员可使用此本机 ERP 兼容模块。

流程：compareBot CLI 图搜 / DINOv2 / 千问 → 审核分流 → 毛子直连商品政策、人工反馈、FBS、重量尺寸及邮政利润核验 → 原有利润、禁止清单和上架流程。ERP 桥接代码保存在 `bridges/comparebot.mjs`，不调用旧的 `verify1688` 分支。不需要 Chrome。

`approved` 才进入 ERP 核验；`manual_review` 进入 attention；`rejected` 淘汰；程序、网络或模型失败沿用任务重试。缺采购价进入人工审核，不估价。DINOv2 分数保留原值，结构分数为空。compareBot 使用交付模块默认 0.75 / 0.55 阈值，旧流程的图片 / 结构阈值仅用于旧 matcher。规格尚未做硬一致性检查，阈值也尚未经过本业务校准。

候选 `origin.size` 可提供 small / large / unknown；缺失默认 unknown，不自动把商品当作小商品。`origin.additional_image_urls`、`origin.specifications` 一并传入。高分但大小未知的商品按附件契约进入人工审核。

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

工作流的 compareBot 密钥栏继续支持旧 ERP Token；使用千问时填写 JSON（由现有工作流密钥加密保存）：

```json
{"erp_token":"YOUR_ERP_TOKEN","dashscope_api_key":"YOUR_QWEN_KEY"}
```

千问密钥不从服务器环境或 `.env` 继承，避免混用用户凭据。无千问密钥时中分商品进入人工审核。利润模块仍使用原 ERP 密钥。

验证使用隔离数据库与模拟 CLI / ERP，不触发商品发布。上线前需在安装好模型的环境对实际商品做只读验收，再部署分支并启动工作流。

## 本次验证

完整测试 102 项通过；随后新增 4 项 CLI、审核分流及 Node 桥接测试，匹配专项共 18 项通过。Node 桥接测试实际执行桥接脚本、模拟 ERP 依赖，验证新采购价进入利润计算且不会调用旧图搜。修改文件 Ruff、JS 语法与 diff 空白检查通过。没有执行真实 API / 模型验收或 Docker 构建。
