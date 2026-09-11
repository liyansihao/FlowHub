# compareBot 1688 匹配接入

分支 `codex/comparebot-1688` 基于 FlowHub 已提交版本 `581421f`，未包含原工作目录的未提交修改。未连接或切换生产数据库、未启动生产工作流。

附件及按本次用户口径的适配修改保存在 `vendor/compareBot`。FlowHub 保留 `flowb-matcher` 模块 ID，数据库初始化时把模块目录的旧 FlowB matcher 切为 compareBot；已有任务中冻结的模块快照不改写。示例流程仍使用 demo。仅管理员可使用此本机 ERP 兼容模块。

流程：compareBot CLI 图搜 / DINOv2 → 新版千问审核分流 → 毛子直连商品政策、人工反馈、FBS、重量尺寸及邮政利润核验 → 原有利润、禁止清单和上架流程。ERP 桥接代码保存在 `bridges/comparebot.mjs`，不调用旧的 `verify1688` 分支。不需要 Chrome。

2026-09-11 按用户最新附件 `compareBot-20260911(1).zip` 更新，撤销之前纯 DINOv2 60% 规则。完整分流：

- DINO < 0.63：淘汰。
- DINO ≥ 0.86 且大小明确为 small：直接通过。
- 其余（包括高分 large / unknown）：千问审核。同款且 DINO ≥ 0.82 通过；不同款且 DINO ≤ 0.64 淘汰；其他人工审核。
- 千问必须比较同一商品本体，明确品牌或型号冲突通过结构化 `brand_or_model_conflict` 字段直接淘汰；颜色、包装、拍摄角度、销售件数不单独否决。一边无品牌或看不清不算明确冲突。
- 千问缺密钥或请求失败进入人工审核。未经千问的高分小商品仍遵循直接通过分支。

附件原生高分大/未知商品直接人工审核，与用户文字要求不同，因此本地调整为送千问，并允许高分千问决策；另增加品牌/型号冲突标记。具体修改见 vendor Git diff。新提示词版本 `same-product-brand-safe-v3-flowhub-conflict`，不复用旧审核结果。

同款通过后仍独立核验 ERP 来源、采购价、利润、禁止清单及重复上架。缺采购价进入人工审核，不估价。旧图片 / 结构阈值只用于旧 matcher。DINO 分数不是识别准确率。

## 大小与利润口径（最新）

小商品必须同时满足毛子 ERP 包裹重量 <500g、人民币售价 <135 元；任一达到上限即为大商品。两项不足且无已知超限项时为 unknown，不用标题、旧 size 标签或卢布原价推断。DINO 图搜后先做只读 ERP 核验取得重量、人民币售价和成本利润率，成本利润率 <25% 立即淘汰（25% 本身不淘汰），无需千问或人工同款审核。利润达标后才按大小、DINO 与千问规则分流。来源核验失败直接淘汰；接口异常保留重试。

纯图搜最多90秒，ERP最多120秒，复用排序结果的审核最多70秒，总限时仍为280秒。不重复加载模型或重新图搜。

## 安装与运行

在独立 Python 3.12 环境安装：

```sh
python3.12 -m venv .venv-comparebot
.venv-comparebot/bin/python -m pip install './vendor/compareBot[search1688,dinov2]' 'python-dotenv>=1.2,<2'
export FLOWHUB_COMPAREBOT_PYTHON="$PWD/.venv-comparebot/bin/python"
# 可选：cpu / mps / cuda
export FLOWHUB_COMPAREBOT_DEVICE=cpu
```

首次加载 DINOv2 会下载模型，建议先按附件 README 预热。分阶段 CLI 与 ERP 总限时 280 秒，小于现有 300 秒任务租约。超时或取消会杀死并回收子进程。部署时需一起分发 vendor 和 bridges 目录；现有 Windows 安装包未重建。

工作流密钥支持旧 ERP Token 或 `{"erp_token":"YOUR_ERP_TOKEN","dashscope_api_key":"YOUR_QWEN_KEY"}` JSON，由工作流加密保存。千问只使用该工作流密钥，经子进程环境传入，不进入命令参数、日志或仓库；屏蔽继承的千问密钥和 dotenv 自动加载。利润模块仍使用原 ERP 密钥。

隔离试用部署地址 `http://127.0.0.1:38428`，图片对照页 `/trial/`。原 FlowHub 生产服务未切换，试用工作流保持暂停，避免自动批量发布。

## 本次验证

完整测试 138 项通过；20 件重审结果为 8 通过、3 淘汰、9 人工审核。覆盖 0.63 / 0.64 / 0.82 / 0.86 精确边界、大小分流、高分非小商品千问、品牌型号冲突、缺密钥/请求失败及 ERP 入口防旧通过记录绕过。现有 20 件样本保留 DINO 图搜结果，重新调用新版千问，结果发布到隔离试用对照页。试用工作流暂停，不自动触发批量上架。
