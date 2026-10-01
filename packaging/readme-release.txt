# COM-Telnet 网关：把 COM 串口发布成 Telnet（多串口 / 多客户端广播）
#
#   COM-Telnet网关.exe        图形界面版，双击即用
#   com-telnet-cli.exe        命令行版（在 cmd/PowerShell 里运行）
#   onedir\                    免解包“绿色版”：整个文件夹一起拷贝，启动更快
#
# 快速开始
#   1) 双击 COM-Telnet网关.exe
#   2) 插上 USB 转串口线，点“刷新”，选 COMx 与波特率
#   3) 点“▶ 启动”，客户端连 本机IP:2323
#      - PuTTY：Connection type 选 Telnet，端口 2323
#      - 命令行客户端：com-telnet-cli.exe --list-ports 查看串口
#
# 多个串口同时转发（多通道）
#   * 界面左侧是“通道列表”，点“＋ 新建”加一个通道：
#     监听端口会自动分配（2323、2324…，跳过被占用的），日志按通道名生成。
#   * 每个通道有自己的“▶ 启动 / ■ 停止”，工具栏的“▶ 全部启动 / ■ 全部停止”一次管全部。
#   * 通道之间完全独立：客户端、写入权限、换行转换、日志文件都互不影响；
#     某个通道启动失败（串口打不开/端口冲突）不影响其它通道。
#   * 保存和启动前会自动检查端口冲突，两个通道抢同一个端口会被拦下。
#
# 命令行示例
#   com-telnet-cli.exe -p COM3 -b 115200 -t 2323
#   com-telnet-cli.exe -p COM3 -t 2323 -p COM5 -t 2324        （两个串口 -> 两个端口）
#   com-telnet-cli.exe --map COM3=2323 --map COM5=2324:9600   （设备=端口[:波特率]）
#   com-telnet-cli.exe --check -p COM3 -t 2323                （只检查配置与端口冲突）
#   com-telnet-cli.exe -p COM3 --raw --single --policy first --log --log-file logs\com3.log
#
# 说明
#   * 无需安装 Python：exe 里已内置运行时。
#   * 单文件版启动时会把运行时解包到临时目录（首次启动约 2-4 秒）；
#     若系统限制了临时目录或杀软误报，请改用 onedir 绿色版。
#   * 配置文件 config.json 与日志会生成在 exe 所在目录。
#   * 局域网访问需放行入站端口（管理员执行一次）：
#     netsh advfirewall firewall add rule name="COM-Telnet 2323" dir=in action=allow protocol=TCP localport=2323
#     多通道可一次放行一段：... localport=2323-2330
#   * Telnet 是明文协议且本程序无认证，请勿直接暴露到公网；跨公网建议走 SSH 隧道。
#
# 完整文档见仓库根目录的 README.md
