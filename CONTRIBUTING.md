# 协作开发

当前能力、限制与优先事项见 [开发状态](docs/DEVELOPMENT_STATUS.md)。请先提交 Issue 描述问题和可验证的结果，再从 `main` 创建 `codex/<topic>` 或个人功能分支，使用 Pull Request 协作。

## 本地验证

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
node --test tests/native-shop.test.mjs tests/storefront-auto.test.mjs
```

单元测试不需要提供真实店铺凭据。真实接口、真实采集与发布测试须使用自己有权限的账号，明确操作范围和限额。不要把测试通过写成真实上架成功。

PR 说明应包含：具体问题、最终行为、验证结果、运行依赖和剩余限制。暂停、断点、幂等、跨店身份和用户隔离的修改应有针对性测试。优先使用毛子 ERP 直连；浏览器依赖应明确标注，不能静默回退。

禁止提交 `.env`、密钥、会话、数据库、账号导出、浏览器配置、原始采集页面、真实商品清单与运行日志。示例使用占位符或合成数据。发现敏感信息请勿复制到公开 Issue。

真实商品写操作必须保留来源、时间、失败原因及回查证据；明确下架清单优先于普通选品规则。不要为了修复状态而盲目重发、补库存或重新上架。
