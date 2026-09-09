# 模块协议 v1

四个插槽：`candidates`（候选池）、`matcher`（同款识别）、`profit`（成本利润）、`publisher`（上架/库存/回查）。管理员注册模块，用户选择并保存自己的模块密钥。更换模块只作用于新任务；已有任务保存当时的模块定义与规则，原店和仓库不改变。

## HTTPS 模块

管理员填写固定公网 HTTPS 地址。调用为 POST，禁止重定向和私网目的地址；连接25秒超时，最大响应2MB。请求头含当前用户填写的 `Authorization: Bearer ...` 和该商品固定的 `Idempotency-Key`（有商品任务时）。不能要求平台服务器全局密钥。

请求：

```json
{"version":"1","operation":"match","context":{"candidate":{"source_key":"123","title":"标题","image":"https://example.com/a.jpg","price":39,"weight_g":200,"dimensions_cm":[20,15,5],"pure_fbs":true},"rules":{"image_min":70,"dhash_min":55,"profit_min":30,"stock":99,"logistics":"ChinaPost","live":true}}}
```

响应包裹统一为 `{"version":"1","result":{...}}`。字段含义如下：

| operation | result 必需内容 |
|---|---|
| candidates | `items`（最多50条候选）、`cursor`（下页游标）；候选必须有 source_key/title/image/price/weight_g/dimensions_cm/pure_fbs；CNY价格、克、厘米 |
| match | supplier_id、supplier_url、image、purchase（CNY）、score和dhash（0–1）、observed_at（Unix秒）；不能返回未绑定具体供货商品的分数 |
| profit | cost_return（百分数，利润÷总成本）、total_cost（CNY）、logistics、route_available、observed_at；可返回 sell_price 用于最终CNY售价 |
| identity | verified=true；上架模块须验证当前 store 的账号和仓库确实匹配 |
| quota | remaining、reset_at（Unix秒）、store_id（传入的内部店铺ID）；过期或不完整结果不视为无限额度 |
| prepare | ready=true/false，及模块提交所需的准备字段；返回结果会以prepared传给publish |
| publish | accepted=true；或 not_sent=true（仅在确认尚未发送/平台明确未接收时）；响应超时后核心只会reconcile，不再次publish |
| reconcile | found、product_id、issue、store_id；必须按传入固定幂等键精确定位 |
| stock | accepted=true；只写指定 store.config.warehouse_id 和 rules.stock，不写其他仓 |
| check_stock | selling、stock、store_id、warehouse_id；仅真实可售且目标仓库存一致计成功 |

上架模块的context额外包含 `store`（id/name/config/credentials）、`idempotency_key`、当前candidate/match/profit/prepared。**接入第三方上架模块会向该模块发送当前店铺的凭据**，只注册自己信任或自己部署的服务。候选/匹配/利润插槽不会接收店铺credentials，仅接收各自模块密钥与业务上下文。

不得把返回 `accepted` 当成可售；不得仅凭提交失败在另一家店重复发布。未确认写入持久化等待，超过24小时转为需处理。所有公开结果必须能审计到具体商品、店铺与仓库。

## 服务器插件

把受信任开发者代码放进 `flowhub_plugins/`；服务器管理员在 `plugins/installed.json` 添加 `{"provider-v1":"flowhub_plugins.provider_v1"}`，再在管理界面以plugin注册这个ID。该目录不通过HTTP提供，用户不能上传或执行任意代码。

入口为：

```python
async def invoke(operation: str, context: dict, credential: str) -> dict:
    ...  # 直接返回上表中的 result 对象
```

示例文件 `flowhub_plugins/example_matcher.py` 故意未实现真实供应商调用，不会伪造分数。插件是服务器受信任代码，具有服务进程权限，**不是不可信代码沙箱**；上生产前需要管理员审查。HTTP方式更适合供应商独立发布与升级。

## 当前 FlowB 兼容模块

本机管理员的真实验收使用 `flowb-candidates`、`flowb-matcher`、`flowb-profit` 三个可选兼容模块，依赖现有工作区的候选来源/匹配历史/API节流与脚本。毛子Token使用当前工作区加密配置，不读取其他用户密钥。它们不会提供给普通用户。这是迁移桥接，并不表示已把所有原始发现算法重写成可独立分发的模块。通用HTTPS/插件插槽和毛子上架适配器不依赖此兼容脚本。
