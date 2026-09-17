# FlowHub Windows 执行端：第一阶段接入验收

当前模式：**acceptance_only**。本包只执行连接/领单/回传测试，不能真实上架，不包含店铺或毛子密钥，也不会调用Chrome。不要运行旧的独立FlowHub刊登脚本来代替执行端，否则不受中央任务互斥保护。

## Windows 安装

1. 安装 Python 3.12 或更新版本（python.org），包含 Python Launcher（py命令）。
2. 安装 Tailscale：https://tailscale.com/docs/install/windows ，加入与Mac中控相同的私有网络。
3. 将本压缩包解压到本地目录。
4. 等中控管理员给出HTTPS地址和30分钟有效的一次性接入码，双击 Setup.cmd。输入中控HTTPS地址和接入码。接入码输入时不显示；成功后配置存到 `%LOCALAPPDATA%\FlowHubAgent`，目录权限限制为当前Windows用户和SYSTEM。
5. 双击 Check.cmd 验证连接，再双击 Start.cmd 持续运行。窗口关闭即停止。
6. 验收通过后，在PowerShell执行：
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Enable-Startup.ps1
   它创建当前用户登录后启动的任务。启用后关闭手动Start.cmd窗口，避免双实例。锁会阻止同一配置重复启动。

## Mac 中控配置

中控服务已在Mac `127.0.0.1:38428` 运行，该地址不能直接填给Windows。

1. Mac安装Tailscale：https://tailscale.com/docs/install/mac ，登录与Windows同一个私有网络；如果系统提示批准网络扩展，请在系统设置完成。
2. 使用本机Tailscale命令，启用仅私有网络可访问的HTTPS反向代理：
   tailscale serve --bg http://127.0.0.1:38428
   tailscale serve status
   App安装方式可能需要以 `/Applications/Tailscale.app/Contents/MacOS/Tailscale` 代替 tailscale。
   如工具提示启用HTTPS，请按它提供的链接完成。不要使用Funnel，也不要映射路由器公网端口。
3. 将Serve显示的 `https://机器名.网络名.ts.net` 地址提供给Windows。局域网与异地均使用这个地址。私有网络内仍需FlowHub独立设备凭证。
4. Windows准备好接入时，在Mac的FlowHub目录执行：
   .venv/bin/python scripts/cluster_control.py invite --name windows-02 --output data/cluster/windows-02-invite.json
   接入码保存在该文件的code字段，30分钟过期且仅可使用一次。不要把它放到公共文档或安装包。若文件已存在，选择新文件名。
5. 创建一次验收任务：
   .venv/bin/python scripts/cluster_control.py probe --key windows-02-first-check
6. 查看设备和结果：
   .venv/bin/python scripts/cluster_control.py status
   查看设备platform是否Windows、last_seen是否更新，以及tasks.done是否增加。
7. 丢失设备或不再使用：
   .venv/bin/python scripts/cluster_control.py revoke --device 对应device_id

## 当前验收范围

已在Mac测试：设备认证、一次性接入、任务唯一认领、重复回传、失联租约接管、旧租约拒绝、撤销设备，以及两个独立客户端进程的HTTP收发。

尚需在真实Windows测试：Python/PowerShell安装、Tailscale连通、设备回传、登录自启、断网重连。

**此阶段完成后还需连接现有生产队列与全局商品/店铺锁、统一毛子调用限速和收藏/草稿占用，才能开启跨机真实上架。当前验收队列不会替代现有生产去重，也不能据此宣称多机已能刊登。**

## 运行与排障

- Mac服务：`com.flowhub.coordinator`，登录用户的LaunchAgent。登录后自启，退出异常自动重启，运行期间caffeinate阻止接电时系统闲置睡眠；不等同于开机未登录也运行。
- Mac数据：FlowHub/data/cluster/cluster.sqlite3；日志：同目录service-error.log。
- 网络中断时Windows以5秒逐步退避至300秒；结果先保存在本地再回传。
- 密钥被撤销时客户端停止。TLS验证不可关闭，不允许远端HTTP地址。
- 接入码提交后若网络中断导致注册结果丢失，请由管理员撤销未使用的新设备并签发新码。
