# 新商品恢复 ERP 跟卖发布

用户明确指定修改当前主 FlowHub，仅替换上架模块。以当前 279832f 为基底，不合并 Personal 的其他代码。

参考实现固定为 ozon 652199132824c732e8b002bc7e40338454046d0a 的 flow_b_process_batch.py，及 ozon-playwright cff47b4c0ba4936b99f3e1345ec4eb656c486746 的 publish-runner.mjs/maozi-client.mjs。发布请求恢复为 /api.selection.follow/import，scene=erp、单店、原水印、source=favorite、CNY 售价。只提交收藏ID、SKU、标题、图片、链接、价格、offer及店铺信息，不提交完整属性包。

## 唯一切换入口

主数据目录 publication-policy.json 的 backend=maozi_follow 选择新商品的跟卖发布路径。店铺配置不改，已有记录优先按自身 backend 路由：official仍走官方回查，旧无backend记录仍走原ERP账本，maozi_follow始终走本次跟卖路径。未部署或backend=existing沿用原新任务选择策略。

新路径复用已审批利润输入，不创建ERP草稿、不运行采集状态机、不映射官方属性、不校验dossier证书、不调用/v3/product/import。队列审核成功后直接进入发布；仅因上架完整资料滞留且尚无发布意图的needs_fields任务回到原审核入口，原补资料状态存入publication_migrations。基础估价资料、同款识别和利润判断不修改，人工审核/隔离/拒绝任务不自动恢复。

发布反馈复用原硬性禁售、品牌、类目holdout和人工反馈规则；明确来源准入与现有comparebot weight-first逻辑一致，不额外要求跟卖请求不使用的完整属性。没有修改共享legacy源码。

## 历史与重复提交

所有原发布、来源、草稿、删除回执保留。已有未知来源写入单独进入人工待处理，禁止被新发布器再次收藏或转草稿。原Offer、店铺、库存目标和精确商品回查不更换。ERP受理不等于stock_verified，原主系统库存验证和成功计数保留。

回退只将新准入策略改为existing；已有maozi_follow记录必须留在本版回查器。不能直接降级到不识别新backend的代码，不能还原旧数据库。唯一主服务受控重启；不启动任何Personal或退役发布器。

## 验证

离线集成使用合成DB和模拟ERP/Ozon响应，禁止测试访问生产写接口。覆盖无完整资料到ERP导入及库存确认、精确请求字段、旧两处资料关口不再触发、响应丢失及策略回退不重发、历史官方/ERP记录不改后端、未知来源写入不重放、估价队列不迁移、利润/审核/禁售规则仍阻断。Node反馈测试验证缺属性可通过且禁售与人工反馈仍生效。

部署及现场证据另存工作区deliverables/FlowHub-follow-publisher-20260919，不以测试数量宣称持续提速或长期稳定验收。
