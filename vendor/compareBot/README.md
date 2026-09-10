# compareBot

compareBot 是一个独立的 **Ozon → 1688 商品视觉匹配模块**。它接收 Ozon
商品信息，调用 1688 以图搜货，使用本地 DINOv2 对候选排序，并在中等相似度时
调用千问视觉模型复核。

```text
Ozon 图片、标题、规格
→ 1688 以图搜货
→ DINOv2 本地排序
→ 高/中/低匹配分流
→ 中匹配由千问复核
→ 输出通过、淘汰或人工审核
```

本项目只负责匹配判断，不负责利润计算、库存、物流或商品上架。

## 1. 模块设计

项目采用 Domain、Application、Port、Adapter、Interface 五层结构：

```text
主工程 / 命令行
       ↓
Interface：读取输入、加载配置、输出 JSON
       ↓
Application：编排图搜、下载、排序、复核
       ↓
Domain：根据分数、商品大小和千问结果作最终决策
       ↓
Port：定义外部能力接口
       ↓
Adapter：实现 1688、DINOv2、千问和 HTTP 图片访问
```

| 目录 | 职责 | 关键内容 |
| --- | --- | --- |
| `src/comparebot/domain/` | 纯数据模型和决策规则，不访问网络 | 输入模型、候选模型、阈值、三级分流 |
| `src/comparebot/application/services/` | 编排一个完整用例 | 图搜与排序、商品筛选 |
| `src/comparebot/application/ports/` | 定义可替换的外部能力 | 图搜、图片加载、排序器、视觉复核协议 |
| `src/comparebot/adapters/` | 对接真实技术实现 | 1688 图搜、DINOv2、千问、HTTP 下载 |
| `src/comparebot/interfaces/` | 程序入口 | 完整筛选 CLI、仅排序 CLI、校准 CLI |
| `src/comparebot/calibration/` | 离线校准工具，不参与生产决策 | 样本采集、人工标注、阈值分析、标注网页 |
| `tests/unit/` | 固定行为与接口契约 | 规则、Adapter 解析、服务编排和校准测试 |

依赖方向只有一条：`Interface → Application → Domain/Port ← Adapter`。业务规则
不会写进 1688 或千问 Adapter，因此以后替换外部平台、模型或主工程时，不需要重写
决策层。

核心执行链：

```text
读取 Ozon 商品
→ 下载 Ozon 主图
→ 1688 以图搜货
→ 并发下载候选图并按 offer_id 去重
→ DINOv2 计算余弦相似度并排序
→ 取第一名进入三级决策
→ 必要时调用千问
→ 输出 approved / manual_review / rejected
```

## 2. 决策原理

DINOv2 分数是图片语义向量的余弦相似度，不是“同款概率”。它适合快速缩小候选，
但对颜色、拍摄角度和同类不同款的区分并不总是可靠。因此程序使用“本地模型快速
筛选 + 千问处理灰区 + 人工兜底”的组合，而不是让单个模型决定所有商品。

| DINOv2 结果 | 商品大小 | 处理方式 | 输出 |
| --- | --- | --- | --- |
| 高匹配（默认 `>=0.86`） | 小商品 | 直接通过 | `approved` |
| 高匹配 | 大商品或未知 | 人工审核 | `manual_review` |
| 中匹配（默认 `0.63–0.86`） | 任意 | 千问复核，并与DINO安全区间组合 | 通过、淘汰或人工审核 |
| 低匹配（默认 `<0.63`） | 任意 | 直接淘汰 | `rejected` |

中匹配区内，千问判定同款且DINO分数至少`0.82`才自动通过；千问判定不同款且
DINO分数至多`0.64`才自动淘汰，其余进入人工审核。阈值来自234条跨品类人工标注，
不等于“同款概率”。

设计原则：

- 自动通过只处理证据较强的小商品，降低错配货源的风险。
- 大商品即使图片高度相似也进入人工审核，避免一次错误带来较高损失。
- 千问只复核 DINOv2 的中匹配灰区，不承担全量图搜和排序。
- 千问、网络或图片下载失败时进入人工审核，不把技术故障当成“不同款”。
- 当前按用户要求不做规格硬审查；品牌或型号明确冲突仍由千问拦截。

## 3. 环境配置

支持 macOS、Windows 和 Linux，需要 Conda。建议至少预留 4 GB 磁盘空间；首次
运行会下载 `facebook/dinov2-small` 模型。

```bash
cd compareBot
conda env create -f environment.yml
conda activate comparebot
```

以后再次使用只需：

```bash
cd compareBot
conda activate comparebot
```

设备参数：

- Apple 芯片 Mac：可使用 `--device mps`
- NVIDIA 显卡：可使用 `--device cuda`
- Windows 普通电脑或无独显机器：使用 `--device cpu`
- 不传 `--device`：程序自动选择

## 4. 配置千问 API

1. 登录[阿里云百炼控制台](https://bailian.console.aliyun.com/)。
2. 在北京地域创建 API Key，并保存生成的密钥。
3. 复制 `.env.example` 为 `.env`。
4. 将密钥写入 `.env`：

```env
DASHSCOPE_API_KEY=你的百炼API密钥
QWEN_VL_MODEL=qwen3-vl-plus
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

程序启动时会自动读取项目根目录的 `.env`。`.env` 已被 Git 忽略，禁止把密钥
发送给他人或提交到仓库。如果不配置密钥，中匹配商品不会调用千问，而会进入
`manual_review`。

## 5. 输入接口

调用方提供一个 JSON 数组作为商品清单。默认读取
`fixtures/ozon_samples.json`。

```json
[
  {
    "product_id": "2008837374",
    "title": "Hair Clip",
    "product_url": "https://www.ozon.ru/product/2008837374/",
    "image_url": "https://example.com/main.jpg",
    "additional_image_urls": [
      "https://example.com/side.jpg",
      "https://example.com/detail.jpg"
    ],
    "specifications": {
      "color": "red"
    },
    "size": "small"
  }
]
```

字段规范：

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `product_id` | 是 | string | Ozon 商品 ID；同一清单内应唯一 |
| `title` | 是 | string | Ozon 商品标题 |
| `image_url` | 是 | string | 主图的公网 URL |
| `additional_image_urls` | 否 | string[] | 补充图片 URL；千问最多读取前4张 |
| `specifications` | 否 | object | 上游规格；当前版本保留但不参与硬判定 |
| `size` | 否 | string | `small`、`large` 或 `unknown`，默认 `unknown` |
| `product_url` | 否 | string | 仅用于追踪原商品 |

接口规则：

- `product_id`、`offer_id` 一律按字符串处理，避免长数字被截断。
- 图片必须是程序可以直接下载的 `https://` 地址；主图不可缺失。
- `additional_image_urls` 应排除主图重复项，程序也会再次去重。
- `size` 由调用方判断；无法判断时传 `unknown`，程序会采用人工审核路径。
- 一次 CLI 调用只处理一个 Ozon 商品；批量、重试和并发由上游队列管理。
- 主工程只依赖稳定的 `decision` 字段，不依赖 Adapter 的原始返回结构。

## 6. 运行完整筛选

```bash
python -m comparebot.interfaces.screen_cli \
  --manifest fixtures/ozon_samples.json \
  --product-id 2008837374 \
  --size small \
  --top-k 10 \
  --output reports/screening-result.json
```

Windows PowerShell 可写成一行：

```powershell
python -m comparebot.interfaces.screen_cli --manifest fixtures/ozon_samples.json --product-id 2008837374 --size small --top-k 10 --output reports/screening-result.json
```

可选参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--manifest` | `fixtures/ozon_samples.json` | 输入清单 |
| `--product-id` | 无 | 必填，要处理的商品 ID |
| `--size` | 清单值或 `unknown` | 商品大小 |
| `--top-k` | `10` | 返回的 DINOv2 候选数量 |
| `--high-threshold` | `0.86` | 高匹配下限 |
| `--medium-threshold` | `0.63` | 中匹配下限 |
| `--qwen-match-min-similarity` | `0.82` | 千问同款可自动通过的DINO下限 |
| `--qwen-mismatch-max-similarity` | `0.64` | 千问不同款可自动淘汰的DINO上限 |
| `--device` | 自动选择 | `cpu`、`mps` 或 `cuda` |
| `--qwen-model` | `qwen3-vl-plus` | 千问模型 ID |
| `--qwen-base-url` | 北京公网兼容地址 | 百炼 OpenAI 兼容接口地址 |
| `--output` | 无 | 必填，结果 JSON 路径 |

只运行“1688图搜＋DINOv2排序”、不调用千问：

```bash
python -m comparebot.interfaces.cli \
  --manifest fixtures/ozon_samples.json \
  --product-id 2008837374 \
  --top-k 10 \
  --output reports/ranking-result.json
```

## 7. 输出接口

调用方主要读取结果文件中的 `decision`：

```json
{
  "outcome": "approved",
  "tier": "medium",
  "reason": "qwen_match_in_safe_band",
  "selected_offer_id": "854435783450",
  "qwen_review": {
    "verdict": "match",
    "confidence": 0.95,
    "reason": "主体结构和关键部件一致",
    "model": "qwen3-vl-plus"
  }
}
```

### 稳定状态

| 字段值 | 含义 | 上游建议动作 |
| --- | --- | --- |
| `approved` | 匹配通过 | 可进入下一业务阶段 |
| `manual_review` | 证据不足或大商品 | 进入人工审核池 |
| `rejected` | 低匹配或千问判定不同款 | 淘汰 |

常见 `reason`：

- `dinov2_high_small_product`
- `dinov2_high_large_or_unknown_size`
- `dinov2_low_similarity`
- `qwen_match_in_safe_band`
- `qwen_mismatch_in_safe_band`
- `qwen_match_outside_safe_band`
- `qwen_mismatch_outside_safe_band`
- `qwen_uncertain_outside_safe_band`
- `qwen_not_configured`
- `qwen_request_failed`

`search_and_rank` 还包含候选列表、DINOv2 分数、图片下载失败数、设备和各阶段
耗时，供审核和调试使用。成功时进程退出码为 `0`；无法完成图搜、模型处理或输入
格式错误时返回非零退出码。

稳定输出契约只有以下字段：

| 路径 | 类型 | 说明 |
| --- | --- | --- |
| `decision.outcome` | string | `approved`、`manual_review` 或 `rejected` |
| `decision.tier` | string | DINOv2 的 `high`、`medium` 或 `low` 分层 |
| `decision.reason` | string | 可供日志和面板显示的机器原因码 |
| `decision.selected_offer_id` | string | 排名第一的 1688 商品 ID |
| `decision.qwen_review` | object/null | 仅实际调用千问时存在 |

`qwen_review` 中会保存 verdict、confidence、reason、模型、token 数、耗时和提示词
版本，便于之后复查决策。候选详情和耗时属于可扩展字段，主工程不应把它们当成
长期固定的业务契约。

## 8. 千问复核规则与提示词

当前提示词版本为 `same-product-brand-safe-v3`，温度为 `0`，最多向模型提供 Ozon
和 1688 各四张图片。实际发送的核心提示词如下，其中两条标题在运行时动态填入：

```text
判断1688候选和Ozon原商品是否是同一个商品本体。
用途或品类相同但不是同一个东西，必须判为mismatch。
若两边明确展示的品牌或型号冲突，也必须判为mismatch；
一边没有品牌或品牌看不清，不算品牌冲突。
颜色、拍摄角度、背景、外包装或销售件数不同，不单独否决。
能确认是同一个东西且无明确品牌错误时判match；
商品本体不同判mismatch，看不清是否同一个东西时判uncertain。
只返回JSON：
{"verdict":"match|mismatch|uncertain","confidence":0到1之间的数字,"reason":"简短中文理由"}。
Ozon标题：{Ozon 商品标题}
1688标题：{1688 候选标题}
```

图片顺序为：提示词与标题 → Ozon 图片 → 1688 图片。程序只接受以下三个 verdict：

| verdict | 语义 | 是否一定形成最终结果 |
| --- | --- | --- |
| `match` | 同一个商品本体，且没有明确品牌/型号冲突 | 否；DINOv2 至少 `0.82` 才自动通过 |
| `mismatch` | 商品本体不同，或存在明确品牌/型号冲突 | 否；DINOv2 至多 `0.64` 才自动淘汰 |
| `uncertain` | 图片或标题不足以确认 | 是；进入人工审核 |

千问 confidence 会记录但当前不作为硬阈值。这样可以避免模型“看起来很自信”时
绕过已校准的 DINOv2 安全区间。

## 9. 主工程接入规范

当前最稳定的接入方式是“清单文件＋命令行＋结果文件”：

```text
主工程写入商品清单
→ 调用 comparebot.interfaces.screen_cli
→ 等待退出码
→ 读取 output JSON
→ 根据 decision.outcome 分流
```

主工程不应直接依赖 Adapter 内部字段，只依赖 `decision` 契约。后续即使替换
1688接口、DINOv2模型或千问版本，主工程也无需改变状态处理逻辑。

推荐的上游状态映射：

```text
approved      → 进入后续利润/上架决策
manual_review → 写入人工审核池，审核结果再反馈给上游
rejected      → 结束该候选，不进入后续流程
进程非零退出 → 技术失败，按上游重试策略处理
```

## 10. 验证安装

```bash
python -m pytest -q
ruff check --no-cache src tests
```

跨12个类目的校准集包含234条人工标签，其中205条可确定。固定测试切分的68条中，
组合规则自动处理34条：自动通过15条、自动淘汰19条，本次样本中误判和漏判均为0；
其余34条进入人工审核。完整结果保存在`reports/calibration/final-analysis.json`。这批
数据参与过提示词迭代，结果用于工程校准，不应理解为对全市场准确率的无偏证明。

## 11. 已知边界

- 当前不执行规格一致性硬检查。
- 1688图搜通常只返回候选封面；只有接口实际提供多图时才会送入千问。
- 网络、1688接口或千问失败时，不应把商品误判为不同款。
- 当前是单商品 CLI；批量并发应由调用方队列控制。
- `calibration` 目录是可选实验工具，不影响核心筛选流程。

更多设计说明见 [docs/screening-flow.md](docs/screening-flow.md)。

跨品类人工标注与阈值校准流程见 [docs/calibration.md](docs/calibration.md)。
