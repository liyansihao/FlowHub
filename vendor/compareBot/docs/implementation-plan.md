# 第一阶段执行计划

## 目标

建立一个独立模块，接收 Ozon 商品图片、标题和规格，通过 1688 图搜召回候选，再用本地 DINOv2 对候选图片排序。

输出只是候选事实和排序结果，不判断最终同款，不参与利润、物流和上架。

## 数据流

```text
MatchRequest
  → SearchAndRankService
      → ImageSearchPort
          → Alibaba1688ImageSearchAdapter
      → 下载并规范化候选图片
      → ImageRankerPort
          → DinoV2RankerAdapter
  → MatchResponse
```

## 里程碑

### M0：1688 图搜可行性验证

- 用 5 张不同类目的真实 Ozon 图片测试图搜。
- 确认能稳定获得 offer ID、标题、商品链接和主图。
- 记录单次耗时、返回数量、验证码和登录依赖。
- 根据结果确定首个 Adapter 使用 SDK、网页会话还是浏览器控制。

验收：至少 4/5 张图片能返回有效候选；失败必须返回明确错误，不能伪装成“无匹配”。

### M1：冻结输入输出契约

- 定义 `MatchRequest`、`SearchCandidate`、`RankedCandidate` 和 `MatchResponse`。
- 输入支持一张主图，并预留多图字段。
- 输出保留原始顺序、DINOv2分数、模型版本和耗时。

验收：调用方只依赖这些契约，不需要知道1688和DINOv2的实现。

### M2：实现 1688 图搜 Adapter

- 上传查询图片并解析候选。
- 按 offer ID 去重。
- 校验图片及商品链接。
- 区分临时错误、访问限制和真实空结果。
- 保存脱敏响应样本作为契约测试夹具。

验收：相同响应样本始终解析出相同结果；平台字段变化会明确报错。

### M3：实现 DINOv2 本地排序

- 统一图片方向、颜色空间和尺寸。
- 模型只加载一次，候选批量推理。
- 对向量做归一化并计算余弦相似度。
- 按图片内容哈希缓存候选向量。
- 支持 CPU、Apple MPS 和 CUDA；结果包含实际设备与模型版本。

验收：固定图片集排序可重复；同图排名第一；批量推理快于逐张推理。

### M4：串联和流出接口

- `SearchAndRankService` 串联图搜、下载和排序。
- 首先提供 Python API 和命令行 JSON 输出。
- 契约稳定后增加独立 HTTP API，不把内部异常直接暴露给调用方。

验收：一条命令输入测试商品并输出 Top K 候选 JSON。

### M5：真实效果验证

- 人工标注至少 200 组“同款/不同款”图片对。
- 按类目统计 1688 召回率、DINOv2 Top-1/Top-5 命中率和耗时。
- 不把 DINOv2 余弦分数解释为同款概率。

验收：先确定基线数据，再决定 Top K 和阈值；不能凭单个样本定阈值。

## 首版对外契约

输入示例：

```json
{
  "request_id": "demo-001",
  "product_id": "ozon-sku-123",
  "title": "商品标题",
  "specifications": {"color": "black"},
  "image_urls": ["https://example.com/ozon.jpg"],
  "top_k": 10
}
```

输出示例：

```json
{
  "request_id": "demo-001",
  "status": "completed",
  "model_version": "dinov2-vits14",
  "raw_candidate_count": 28,
  "candidates": [
    {
      "offer_id": "1688-offer-id",
      "title": "1688商品标题",
      "offer_url": "https://detail.1688.com/offer/xxx.html",
      "image_url": "https://example.com/1688.jpg",
      "dinov2_similarity": 0.8732,
      "rank": 1
    }
  ],
  "timing_ms": {
    "image_search": 1200,
    "image_download": 500,
    "dinov2_ranking": 180
  }
}
```

## 明确不做

- 不把标题和规格混入 DINOv2 图片分数。
- 不在本阶段判断自动上架、人工审核或淘汰。
- 不接入 Qwen。
- 不依赖 FlowEF 的数据库、队列或业务模型。
- 不复制第三方私有源码、提示词或凭证。

## 主要风险

1. 1688 图搜访问方式可能受登录、验证码或接口变化影响。
2. 商品主图可能包含模特、文字、拼图和不同背景，DINOv2需要真实数据验证。
3. 同一1688商品的封面可能不对应目标规格，因此本阶段输出只能是“候选排序”。
4. 首次下载模型体积较大，Mac、Windows和GPU环境需要分别验证。

