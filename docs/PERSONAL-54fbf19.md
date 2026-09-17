# FlowHub Personal · 54fbf19

基准提交：54fbf191d154f010ea05e4aa248900e4d394f3e7（compareBot 匹配版本）。增加个人版启动/店铺勾选、桌面启动适配和 Windows 依赖打包。并非将当前主分支改名为此版本。

## 当前 Mac

打开「FlowHub Personal 54fbf19.app」，应用连接本机个人版 http://127.0.0.1:38437 ，后台由本机登录服务启动并守护，应用可唤起。登录原 FlowHub 账号密码，已有毛子 ERP 连接与店铺已在这台电脑私密导入，无需重复录入 ERP 密钥。

原工作台 38427 保留。个人版任务队列独立，原工作台已有 114 个商品条目作为禁止重复处理记录导入；原工作台的任务没有删除。个人版保持暂停，候选上限 100、本次提交上限 3；用户勾选店铺并启动才执行真实任务。不要在旧版和个人版同时对同一家店启动上架。

本 Mac 的 Python 环境和旧流程依赖已安装到 ~/Library/Application Support/FlowHub Personal 54fbf19 的 runtime/legacy 目录，模型使用本机缓存；此 Mac 应用包是已经配置好的本机入口，不是可在任意另一台 Mac 上脱离后台独立运行的安装器。

账号及连接私密目录：~/Library/Application Support/FlowHub Personal 54fbf19/backend-data。不要将此目录放入通用安装包或发给他人。关闭窗口不会等同于暂停后台，停止新增请使用页面的暂停按钮。

## Windows 测试

1. 配套后台包需 Windows 的 Docker Desktop Linux 容器环境，解压到 C:\FlowHubPersonal54fbf19 后运行 Install.cmd，首次需下载依赖。
2. 本包已补上 vendor/compareBot、bridges，以及 search1688、DINOv2 和 transformers 依赖；不能使用以前缺这些文件的 Windows 后台包。
3. 运行 PersonalCheck.cmd 验证服务确实为 54fbf19；运行 ModelPrepare.cmd 下载并预热模型；Check.cmd 检查基础环境。
4. 再安装桌面 exe，连接 Windows 本机 38427。exe 是工作台入口，不能代替上述后台环境。
5. 通用 Windows 包不带 Mac 账号数据库、ERP/Ozon/飞书密钥。首次通过 Admin.cmd 获取本机账号，再配置本人同一个 ERP 的连接资料和 Feishu 禁止清单。正式切换时应重新同步最新商品占用/禁售记录并停用旧电脑该店铺流程。

尚未获得 Windows 测试机，因此 Windows 实机安装、容器构建和真实连通验收未完成，不能以生成 exe/zip 代替通过。

## 验证记录

- 后台 109 项测试通过，含店铺选择、账号隔离和 compareBot。
- DINOv2 指定模型在 Mac MPS 成功加载。
- 使用现有凭据只读核验毛子 ERP 及店铺目标仓库：通过。
- 实际商品只读图搜/排序约 10 秒返回；中分且未配置千问密钥时进入 manual_review（qwen_not_configured），没有伪造审核通过。
- 本次没有执行真实 Ozon 商品提交、改价或补库存。
- 桌面 Mac/Windows 打包成功；未签名/公证。Windows 实机测试待提供环境。

登录守护配置：~/Library/LaunchAgents/com.flowhub.personal54fbf19.plist。原工作台守护服务未更改。
