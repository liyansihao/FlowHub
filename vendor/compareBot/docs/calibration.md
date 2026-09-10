# 相似度校准实验

本实验使用240个真实Ozon商品，覆盖12个类目。排除上衣、外套、裤子等普通服装，
保留袜子和内裤。人工标签是唯一真值；审核页面不显示DINOv2分数或千问结果。

## 运行顺序

```bash
conda activate comparebot

comparebot-calibration build \
  --source-root /path/to/readonly-reports \
  --output reports/calibration/manifest.json \
  --per-category 20

comparebot-calibration collect \
  --manifest reports/calibration/manifest.json \
  --cases-dir reports/calibration/cases \
  --top-k 5

comparebot-calibration summary \
  --manifest reports/calibration/manifest.json \
  --cases-dir reports/calibration/cases \
  --output reports/calibration/collection-summary.json

comparebot-calibration serve \
  --cases-dir reports/calibration/cases \
  --labels reports/calibration/labels.json \
  --port 8770
```

在 `http://127.0.0.1:8770` 完成盲标后：

```bash
comparebot-calibration analyze \
  --cases-dir reports/calibration/cases \
  --labels reports/calibration/labels.json \
  --output reports/calibration/dino-analysis.json

comparebot-calibration qwen \
  --cases-dir reports/calibration/cases \
  --labels reports/calibration/labels.json \
  --analysis reports/calibration/dino-analysis.json \
  --output reports/calibration/qwen-predictions.json \
  --concurrency 2

comparebot-calibration analyze \
  --cases-dir reports/calibration/cases \
  --labels reports/calibration/labels.json \
  --qwen-predictions reports/calibration/qwen-predictions.json \
  --output reports/calibration/final-analysis.json
```

阈值只在校准集选择，测试集只评估一次。默认约束为高匹配自动通过准确率至少97%、
低匹配误淘汰率至多5%，并在满足约束后最大化自动处理比例。千问不会单独做最终
决定；分析器还会校准千问同款和不同款各自可自动处理的DINO安全区间。
