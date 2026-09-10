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

## 决策逻辑

| DINOv2 结果 | 商品大小 | 处理方式 | 输出 |
| --- | --- | --- | --- |
| 高匹配（默认 `>=0.75`） | 小商品 | 直接通过 | `approved` |
| 高匹配 | 大商品或未知 | 人工审核 | `manual_review` |
| 中匹配（默认 `0.55–0.75`） | 任意 | 千问标题＋图片复核 | 根据千问结果决定 |
| 低匹配（默认 `<0.55`） | 任意 | 直接淘汰 | `rejected` |

这些阈值是可运行初值，不等于“同款概率”。正式大规模使用前仍应利用人工标注
数据校准。

## 1. 环境配置

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

## 2. 配置千问 API

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

## 3. 输入接口

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

## 4. 运行完整筛选

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
| `--high-threshold` | `0.75` | 高匹配下限 |
| `--medium-threshold` | `0.55` | 中匹配下限 |
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

## 5. 输出接口

调用方主要读取结果文件中的 `decision`：

```json
{
  "outcome": "approved",
  "tier": "medium",
  "reason": "qwen_match",
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
- `qwen_match`
- `qwen_mismatch`
- `qwen_uncertain`
- `qwen_not_configured`
- `qwen_request_failed`

`search_and_rank` 还包含候选列表、DINOv2 分数、图片下载失败数、设备和各阶段
耗时，供审核和调试使用。成功时进程退出码为 `0`；无法完成图搜、模型处理或输入
格式错误时返回非零退出码。

## 6. 主工程接入规范

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

## 7. 验证安装

```bash
python -m pytest -q
ruff check --no-cache src tests
```

已验证的真实中匹配案例：Ozon `2008837374` 的 DINOv2 分数为 `0.616861`，
千问判定 `match`、置信度 `0.95`，最终输出 `approved`。

## 已知边界

- 当前不执行规格一致性硬检查。
- 1688图搜通常只返回候选封面；只有接口实际提供多图时才会送入千问。
- 网络、1688接口或千问失败时，不应把商品误判为不同款。
- 当前是单商品 CLI；批量并发应由调用方队列控制。
- `calibration` 目录是可选实验工具，不影响核心筛选流程。

更多设计说明见 [docs/screening-flow.md](docs/screening-flow.md)。
