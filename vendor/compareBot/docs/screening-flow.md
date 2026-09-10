# 完整筛选流程

## 决策表

| DINOv2等级 | 商品大小 | 后续动作 | 最终状态 |
| --- | --- | --- | --- |
| 高 | 小 | 不调用千问 | 通过 |
| 高 | 大或未知 | 不调用千问 | 人工审核 |
| 中且DINO >= 0.82 | 任意 | 千问判断同款且无明确品牌冲突 | 通过 |
| 中且DINO <= 0.64 | 任意 | 千问判断不同款 | 淘汰 |
| 其他中匹配 | 任意 | 千问结论不在安全区或调用失败 | 人工审核 |
| 低 | 任意 | 不调用千问 | 淘汰 |

```text
Ozon输入
  → 1688图搜
  → DINOv2排序
  → 取最高分候选
      ├─ 高匹配 + 小商品 → approved
      ├─ 高匹配 + 大/未知 → manual_review
      ├─ 中匹配 → Qwen
      │   ├─ match + DINO >= 0.82 → approved
      │   ├─ mismatch + DINO <= 0.64 → rejected
      │   └─ 其他结论/调用失败 → manual_review
      └─ 低匹配 → rejected
```

## 图片输入

Ozon清单可以提供一张主图和多张补充图片：

```json
{
  "product_id": "123",
  "title": "商品标题",
  "image_url": "https://example/main.jpg",
  "additional_image_urls": [
    "https://example/side.jpg",
    "https://example/detail.jpg"
  ],
  "size": "small"
}
```

1688图搜返回多图字段时，Adapter会保留补充图片。当前真实图搜接口通常只返回
候选封面，此时千问会使用所有实际可用的图片，不会伪造缺失图片。后续可以新增
1688详情页媒体Adapter，而不需要修改Domain或Qwen Adapter。

## 稳定输出

结果中的`decision`是调用方需要读取的稳定字段：

```json
{
  "outcome": "approved",
  "tier": "high",
  "reason": "dinov2_high_small_product",
  "selected_offer_id": "744001904508",
  "qwen_review": null
}
```

完整DINOv2候选和耗时仍保存在`search_and_rank`中，便于审核和阈值校准。
