# FlowHub 毛子 ERP 本机凭证更新工具

每台电脑自己操作：在 **Google Chrome 的普通窗口**登录 https://ozon.maozierp.com/#/dashboard ，看到概览页后，双击该电脑的更新入口。不需要复制 Token、发送密码或打开开发者工具。

## Mac

1. 解压整个工具包，保留其中的文件。
2. 本机已安装 FlowHub 后台（包含 Python 运行环境及已配置的店铺）。
3. 双击 `Refresh-Mac.command`；看到“完成：连接已验证，凭证已更新或已是最新”即成功。
4. 若系统没有直接执行，可在终端输入 `bash `，把 `Refresh-Mac.command` 拖进终端后回车。

默认识别 `~/Library/Application Support/FlowHub*/backend-data`。发现多套数据目录时不会猜选，需要在 `config.json` 的 `data_dir` 填入目标目录。自定义运行环境可设置环境变量 `FLOWHUB_PYTHON` 为 FlowHub 的 Python 可执行文件。

## Windows

1. 本机已安装配套的 FlowHub Windows **Docker 后台包**，Docker Desktop 正常运行。
2. 将本工具包文件夹放在后台目录里面，例如 `C:\FlowHub\CredentialRefresh\`；上一级应有 `compose.yaml` 和 `Start.cmd`。
3. Chrome 登录毛子 ERP 后，双击 `Refresh-Windows.cmd`。
4. 找不到后台目录时，会询问该目录；也可在 `config.json` 的 `docker_dir` 设置它。

Windows 入口复用已安装后台镜像中的 Python，不要求另外安装 Python。它只运行一次凭证更新进程，直接使用已有后台数据卷；不会运行后台的初始化或启动流程，也不会自动下载镜像。可在现有容器停止时使用，但 Docker Desktop 必须启动。

对于自行部署的 Windows 原生 Python 后台，可同时填写 `python_executable`、`data_dir`，入口将改用该环境。只安装了 FlowHub 桌面壳、后台部署在远程服务器的机器不属于本工具适用范围。

## 固定流程

读取 Chrome 当前有效记录 → 核对原毛子账号 → 调用毛子店铺查询接口 → 核对每家店铺的毛子 ID 和 Ozon Client ID → 加密备份旧配置 → 在一个事务中更新店铺和模块凭证 → 数据库回查。

仅刷新已经接入 FlowHub 的账号。首次接入仍需在 FlowHub 配置店铺以及 Ozon API Key 等资料。本工具不创建账号或导入他人的店铺。

保持工作流开关、任务、库存、排期和其他密钥原状。正在运行的工作流在下一次读取凭证时使用新值；已失败并停止的任务不会被本工具自动重跑。不会改 Chrome 登录状态，也不会更新系统钥匙串或其他工程的凭证。

## 配置与故障

`config.json` 不存凭证，所有空值表示自动识别：

| 字段 | 用途 |
| --- | --- |
| `data_dir` | 原生后台存有 `flowhub.sqlite3` 和 `master.key` 的目录；Docker 模式固定为容器内 `/data/app` |
| `chrome_root` | 自定义 Chrome User Data 根目录，里面是 Default、Profile 1 等目录 |
| `profile` | 多个 Chrome 用户时填 `Default` 或 `Profile 1`，不是界面昵称 |
| `docker_dir` | Windows 后台 `compose.yaml` 所在目录 |
| `python_executable` | Windows 自行部署原生后台时使用的 Python 路径 |

Windows JSON 路径建议使用正斜杠，如 `C:/FlowHub`，避免反斜杠转义问题。

- **只想检查**：双击 `Check-Mac.command` / `Check-Windows.cmd`，会验证远端连接但不写配置。
- **登录文件正在变化或读取失败**：正常退出 Chrome 后重试，工具能离线读取已保存的登录记录，不需要 Chrome 正在运行。
- **未找到有效凭证**：退出毛子账号并重新登录，确保不是无痕窗口；确认使用 Google Chrome。
- **账号不匹配 / 多账号**：用原接入账号登录；设置对应 `profile`，防止更新到其他账号。
- **店铺身份不匹配**：工具整批停止，不做部分更新。先核查 FlowHub 的店铺绑定。
- **网络错误**：确保本机或 Docker 可直连 `https://api.maozierp.com`；工具不会通过网页绕过接口错误。
- **换电脑**：工具包可以直接复制，但目标电脑必须已有自己的 FlowHub 后台及店铺配置。本工具不迁移店铺配置。

备份在目标数据目录的 `credential-backups` 下，仅含原有加密字段。主密钥仍留在该电脑。更新工具不会把明文 Token 写到文件、日志或命令行，也不会发送给其他电脑；网络请求只用于毛子官方接口认证与店铺核验。

## 验证范围

Mac 本机真实读取和接口验证、凭证更新/重复运行、解析器及原子更新测试见随包 `VALIDATION.md`。Windows 入口按现有 Docker 后台布局实现；尚未完成 Windows 实机验证，首次使用建议先运行 `Check-Windows.cmd`。

Chrome 本地存储格式或毛子登录字段发生变化时，工具会停止并提示，不会直接写入猜测的凭证。
