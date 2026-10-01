# COM 串口 → Telnet 网关

[![构建 Windows exe](https://github.com/wdj2613/com_to_telnet/actions/workflows/build-exe.yml/badge.svg)](https://github.com/wdj2613/com_to_telnet/actions/workflows/build-exe.yml)

把本机（或网络上的）串口变成 Telnet 服务：**多个客户端可以同时连上来观察同一个串口的数据**，
其中一个客户端（可配置）还能把键盘输入写回串口。纯 Python 标准库实现，**不需要安装任何第三方包**。

支持**多路串口同时转发（多通道）**：COM3 → 2323、COM5 → 2324 …… 每个通道各自独立
（自己的客户端组、写入权限、换行转换、日志文件），可以单独启停，也可以一键全部启停。

```
     串口设备 (COM3)           串口设备 (COM5)
          │  USB / RS232           │  USB / RS232
          ▼                        ▼
   ┌──────────────────────────────────────┐         ┌────────────────┐
   │         本程序（网关，多通道）          │◄───────►│  Telnet 客户端  │
   │   COM3 ⇄ 0.0.0.0:2323                │   :2323 │  PuTTY / telnet │
   │   COM5 ⇄ 0.0.0.0:2324                │   :2324 │  每个通道可多个  │
   └──────────────────────────────────────┘         └────────────────┘
```

---

## 1. 快速开始

### 1.1 图形界面（推荐）

双击 `run.bat`，或者在命令行里：

```powershell
cd D:\MyCode\com_to_ssh
.\.venv\Scripts\python.exe com_telnet_bridge.py
```

界面里选好**串口**和**波特率**，点 **▶ 启动**，然后把 Telnet 客户端连到 `本机IP:2323` 即可。

### 1.2 多个串口同时转发（多通道）

左侧是**通道列表**（状态 / 名称 / 串口 / 监听端口），右侧是**所选通道的参数**，每个通道有自己的
**▶ 启动 / ■ 停止**，工具栏上的 **▶ 全部启动 / ■ 全部停止** 一次管所有通道。

```
通道列表                     所选通道的参数
┌──────────────────────────┐ ┌───────────────────────────────────────────────┐
│ ● 运行中  机柜A  COM3  0.0.0.0:2323 │ 通道名 [机柜A]  ▶ 启动  ■ 停止  运行中 · 客户端 2 │
│ ○ 未启动  机柜B  COM5  0.0.0.0:2324 │ 串口 / Telnet 服务端 / 转换与日志 三栏参数      │
└──────────────────────────┘ └───────────────────────────────────────────────┘
```

* **＋ 新建**：加一个通道，监听端口自动往 2323、2324… 顺延，并且跳过已被占用的端口；
  日志文件自动按通道名生成（`logs/通道2.log`）。
* **⧉ 复制**：照搬当前通道的参数，自动换名字、换端口、换日志文件。
* **✕ 删除 / ↑ 上移 / ↓ 下移**：至少保留一个通道。
* **数据日志**每行都带 `[通道名]` 前缀，客户端列表有「通道」列；
  勾上「只看当前通道」就只显示所选通道的客户端。
* 保存配置（和启动前）都会**自动做端口冲突检查**：两个通道要抢同一个端口会明确提示，
  不允许启动（Windows 上第二个绑定会静默成功，连接随机落到其中一个通道，非常难排查）；
  两个通道用同一个串口只提示、不阻断。

### 1.3 命令行 / 后台常驻

```powershell
# 把 COM3 @115200 发布到 0.0.0.0:2323，日志写文件
.\.venv\Scripts\python.exe com_telnet_bridge.py -n -p COM3 -b 115200 -t 2323 --log --log-file logs\com3.log

# 多个串口 -> 多个端口（-p / -t 按顺序配对，不写 -t 会自动分配 2323、2324…）
.\.venv\Scripts\python.exe com_telnet_bridge.py -n -b 115200 -p COM3 -t 2323 -p COM5 -t 2324

# 设备=端口[:波特率] 写法，socket:// 里带冒号也不会歧义
.\.venv\Scripts\python.exe com_telnet_bridge.py -n --map COM3=2323 --map socket://192.168.1.50:4001=2324:9600

# 只检查通道配置与端口冲突，不启动
.\.venv\Scripts\python.exe com_telnet_bridge.py -n --check -p COM3 -t 2323 -p COM5 -t 2324
```

不给 `-p/--map` 时直接使用 `config.json` 里保存的全部通道。

### 1.4 手上没有串口？先用内置回环口试

```powershell
.\.venv\Scripts\python.exe com_telnet_bridge.py -n -p loop:// -t 2323 --banner "测试网关"
.\.venv\Scripts\python.exe tools\telnet_probe.py 127.0.0.1 2323 --send "hello"

# 两个回环口，验证多通道互不串台
.\.venv\Scripts\python.exe com_telnet_bridge.py -n -p loop:// -t 2323 -p loop:// -t 2324
```

`loop://` 是进程内回环口：写进去的字节会立刻被读回来，用来验证整条链路。

---

## 2. 客户端怎么连

| 客户端 | 连接方式 |
| --- | --- |
| 自带测试工具 | `python tools\telnet_probe.py 127.0.0.1 2323` |
| Windows telnet | 先 `dism /online /Enable-Feature /FeatureName:TelnetClient`，再 `telnet 192.168.1.10 2323` |
| PuTTY | Connection type 选 **Telnet**（勾选 Force no echo 可避免本地回显重复），Host Name 填 `ip`，Port 填 `2323` |
| MobaXterm / SecureCRT | 新建 Telnet 会话，端口 `2323` |

> 如果客户端把输入回显了两遍，或者完全看不到自己输入的内容，在界面上调整 **「服务端回显」** 开关。
> 默认关闭（由客户端本地回显），兼容性最好。

> 多通道时每个通道是**独立的端口和客户端组**：连 `2323` 只看到通道 1 的串口，连 `2324` 只看到通道 2 的串口，
> 互不干扰；某个通道的客户端也不会收到别的通道的数据。

---

## 3. 界面 / 参数说明

窗口从上到下是：**工具栏 → 通道列表 + 所选通道的参数 → 数据日志 + 已连接客户端 → 发送栏 → 状态栏**。

### 通道

| 控件 | 说明 |
| --- | --- |
| 通道列表 | 每行一个通道：状态（`●` 运行中 / `○` 未启动 / `!` 启动失败）、名称、串口、监听端口 |
| ＋ 新建 | 新增通道，自动分配名字、监听端口（2323、2324… 跳过被占用的）和日志文件 |
| ⧉ 复制 | 复制当前通道的参数到新通道（串口会一并复制，注意别让两个通道抢同一个串口） |
| ✕ 删除 / ↑ 上移 / ↓ 下移 | 调整通道；至少保留一个通道 |
| 通道名 | 只用于界面、日志前缀和默认日志文件名 |
| ▶ 启动 / ■ 停止 | 只作用于**当前所选通道** |
| ▶ 全部启动 / ■ 全部停止 | 作用于所有通道；某个通道启动失败（串口被占、端口冲突）不影响其它通道 |

### 串口
串口下拉框可自动列出 `COM1…COMn` 与 USB 转串口设备（如 `USB-SERIAL CH340 (COM3)`）；
也可以直接手输 `COM7`、`loop://`、`socket://192.168.1.50:4001`。
波特率、数据位、校验位、停止位、流控（RTS/CTS、DSR/DTR、XON/XOFF）按设备手册设置。

### Telnet 服务端
| 选项 | 说明 |
| --- | --- |
| 监听地址 | `0.0.0.0` 表示所有网卡（允许别人远程连），`127.0.0.1` 只允许本机连 |
| 端口 | 默认 `2323`（Telnet 标准端口 23 在 Windows 上通常需要管理员权限，故默认 2323）；每个通道一个端口，不能重复 |
| 模式 | **Telnet**：做选项协商、转义 `0xFF`；**裸 TCP**：原样透传，适合 `nc`/自定义程序 |
| 允许多客户端 | 勾选后所有客户端同时收到串口数据（广播）；不勾选则只允许一个，其余被拒绝 |
| 写入权限 | `仅第一个客户端可写`（默认，推荐）/ `所有客户端都可写` / `全部只读` |
| 服务端回显 | 由网关把客户端输入回显回去（部分 Telnet 客户端需要） |
| 欢迎语 | 连接后发送的提示，支持占位符 `{port}` `{baud}` `{peer}` `{writable}` 和 `\r\n` |

### 转换与日志（每个通道一套）
| 选项 | 说明 |
| --- | --- |
| 客户端→串口 换行 | 把回车键产生的换行统一成设备需要的行结束符：`CR`(默认，思科类设备)、`LF`、`CRLF`、`CR NUL`、不转换 |
| 串口→客户端 归一化 CRLF | 把设备输出的裸 `CR`/`LF` 补成 `CRLF`，避免 Telnet 里出现"阶梯状"错行 |
| 编码 | 日志与欢迎语的文本编码：`utf-8` / `gbk` / `ascii` |
| 显示接收 (RX) / 显示发送 (TX) | 这个通道的收发数据是否打进日志窗口 |
| 十六进制 | 这个通道的日志窗口/文件里额外显示 HEX，便于看二进制协议 |
| 写日志文件 / 路径 | 追加写入，默认按通道名生成 `logs/通道名.log`，各通道互不覆盖 |

日志框上方是**所有通道共用**的显示项：`时间戳`、`自动滚动`、`只看当前通道`（客户端列表只看所选通道）。

界面底部可以**直接向所选通道的串口发送**内容：勾选 `HEX` 时输入 `41 54 0D` 这样的十六进制即可。

### 界面缩放 / 高 DPI

界面会根据自己的实际宽度和系统缩放自动调整，不需要手动设置：

| 情况 | 表现 |
| --- | --- |
| 系统缩放 125% / 150% | 程序声明 DPI 感知并按真实 DPI 渲染，字迹清晰不变形（不会像老程序那样被系统放大成糊的） |
| 窗口拉宽 | 通道列表在左侧，参数区串口 / 服务端 / 转换与日志三栏等宽伸展，日志区自动占满剩余空间 |
| 窗口收窄（< 约 1000 px） | 参数区自动变成两栏，再窄则**纵向堆叠**；通道列表挪到参数区上方，客户端列表从右侧移到日志下方，按钮文字自动缩短，不会被裁掉 |
| 字号缩放 | `Ctrl` + 鼠标滚轮，或 `Ctrl` `+` / `Ctrl` `-` / `Ctrl` `0`，或用状态栏右下角的 `A－` `100%` `A＋`；范围 70%–200% |
| 关闭再打开 | 记住上次的窗口大小、位置、字号缩放和所选通道（存在 `config.json`） |

窗口有最小尺寸（按 DPI 换算），并且当参数区堆叠显示时会自动抬高最小高度，保证日志和客户端列表不会被压成一条缝。

### config.json 长这样

```json
{
  "version": 2,
  "ui_scale": 1.0,
  "window_geometry": "1475x900+543+180",
  "selected_channel": 0,
  "timestamps": true,
  "autoscroll": true,
  "channels": [
    { "name": "机柜A", "port": "COM3", "baudrate": 115200, "listen_host": "0.0.0.0",
      "listen_port": 2323, "log_to_file": true, "log_file": "logs/机柜A.log" },
    { "name": "机柜B", "port": "COM5", "baudrate": 9600, "listen_host": "0.0.0.0",
      "listen_port": 2324, "log_to_file": false, "log_file": "logs/机柜B.log" }
  ]
}
```

> 旧版本（单串口）的 `config.json` 会被**自动迁移成第一个通道**，参数（波特率、监听端口、日志路径、
> 界面缩放等）都会保留，不需要手工改。

---

## 4. 目录结构

```
com_to_ssh/
├─ com_telnet_bridge.py      # 入口（不带参数=图形界面，-n=命令行）
├─ run.bat                   # Windows 一键启动（自动用 .venv 虚拟环境）
├─ build_exe.bat / .ps1      # 一键打包成 exe
├─ requirements.txt          # 无第三方依赖
├─ config.json               # 启动时自动保存/读取的配置（v2：多通道）
├─ dist/                     # 编译好的 exe（见第 10 节）
├─ vendor/                   # 离线安装 PyInstaller 用的 wheel
├─ packaging/                # 打包入口脚本、图标生成、版本资源
│  ├─ exe_gui.py / exe_cli.py
│  ├─ make_icon.py / check_icon.py
│  ├─ readme-release.txt     # 随 exe 一起发出的「说明.txt」
│  ├─ fetch_wheels.py
│  └─ app.ico · version_info.txt
├─ comtel/
│  ├─ config.py              # 配置模型与校验（ChannelConfig / BridgeConfig、冲突检查、旧配置迁移）
│  ├─ serial_backend.py      # 串口后端：Win32 API(ctypes) / loop:// / socket://
│  ├─ serial_link.py         # 串口读写线程
│  ├─ telnet_server.py       # Telnet 服务端、协商、多客户端管理
│  ├─ gateway.py             # 串口 ⇄ Telnet 的粘合层与日志（一个通道一个实例）
│  ├─ manager.py             # 多通道管理：增删通道、单独/批量启停、故障隔离
│  ├─ gui.py                 # Tkinter 界面（通道列表 + 详情，含缩放 / 自适应布局）
│  ├─ ui_scale.py            # 高 DPI 感知与字体缩放辅助
│  ├─ cli.py                 # 命令行模式（多通道）
│  └─ console.py             # 控制台 UTF-8 处理
├─ tools/
│  ├─ telnet_probe.py        # 自带的 Telnet 测试客户端
│  ├─ capture_ui.ps1         # 按不同尺寸给界面截图（核对缩放效果）
│  └─ check_ci.py            # 查询 GitHub Actions 最近一次运行状态与产物
└─ tests/
   ├─ selftest.py            # 功能自测（无需真实串口）
   └─ gui_smoke.py           # 界面冒烟 + 缩放/重排测试
```

---

## 5. 命令行参数

```
-n, --no-gui           强制命令行模式
-p, --port COM3        串口名，可重复：-p COM3 -p COM5（也支持 loop://  socket://host:port）
-t, --tcp-port 2323    监听端口，可重复，与 -p 按顺序配对；没写则自动分配 2323、2324…
    --map COM3=2323    一次给一个通道指定「设备=端口[:波特率]」，可重复；与 -p/-t 不混用
    --name 机柜A       通道名，可重复，按顺序对应（默认取串口名）
-b, --baud 115200      波特率（所有通道共用，--map 里可单独指定）
    --bytesize 8       数据位 5/6/7/8
-P, --parity N         校验位 N/E/O/M/S
    --stopbits 1       停止位 1/1.5/2
    --flow  none       流控 none/rtscts/dsrdtr/xonxoff
-H, --listen-host      监听地址（默认 0.0.0.0，所有通道共用）
    --raw              裸 TCP 模式
    --single           每个通道只允许一个客户端
    --policy first     写入权限 first/all/none
    --server-echo      服务端回显
    --banner "文本"    欢迎语
    --tx-newline cr    客户端→串口换行 asis/cr/lf/crlf/crnul
    --no-rx-crlf       串口→客户端不做 CRLF 归一化
    --encoding utf-8   文本编码
    --log --log-file   写日志文件；多通道时自动加 -1/-2 后缀，--hex-log 附带 HEX
    --quiet            只显示状态，不显示收发内容
    --check            只检查通道配置与端口冲突，不启动
-c, --config 路径      配置文件（默认程序目录下 config.json）
    --list-ports       列出本机串口与配置里的通道
-v, --version          版本
```

命令行参数会覆盖配置文件里的同名项；不给 `-p/--map` 时直接运行 `config.json` 里保存的全部通道。
多通道运行时每行日志都带 `[通道名]` 前缀：

```
[机柜A] 22:01:24 [TX] AT\r\n
[机柜B] 22:01:25 [RX] OK\r
```

---

## 6. 让局域网/外网连进来

1. 监听地址填 `0.0.0.0`（默认已是），重启网关。
2. 放行防火墙入站端口（管理员 PowerShell，只需执行一次）：

```powershell
# 只有一个通道
netsh advfirewall firewall add rule name="COM-Telnet 2323" dir=in action=allow protocol=TCP localport=2323

# 多个通道：一次放行一段端口（例如 2323-2330）
netsh advfirewall firewall add rule name="COM-Telnet 2323-2330" dir=in action=allow protocol=TCP localport=2323-2330
```

3. 查本机 IP：`ipconfig`，客户端连 `192.168.x.x:2323`（第二个通道就是 `:2324`，以此类推）。

> ⚠️ Telnet 是**明文**协议，且本程序没有认证。请只在可信内网使用；
> 需要跨公网时，建议先建 SSH 隧道（例如 `ssh -L 2323:127.0.0.1:2323 user@跳板机`），
> 而不是把 2323 直接映射到公网。多通道就每个端口各建一条隧道。

---

## 7. 常见问题

**打不开串口：拒绝访问 / 找不到文件**
换一根 USB 口、换线，或用设备管理器确认 COM 号；不要和其它串口工具（SecureCRT、串口助手）同时打开同一个口。
错误信息会直接显示在日志窗口和状态里。

**端口被占用（10048）**
换一个监听端口，或先关掉占用 2323 的程序：`netstat -ano | findstr :2323`。
多通道时如果两个通道填了同一个端口，界面在保存/启动前就会拦住并说明是哪两个通道。

**两个串口能不能同时用同一路数据？**
不能，但也不需要：每个通道完全独立，两个客户端分别连 2323 和 2324 就各自看到自己那路串口。

**多通道时日志混在一起不好看**
每行都有 `[通道名]` 前缀，也可以用「只看当前通道」过滤客户端列表；
要彻底分开就在每个通道里勾上「写日志文件」，各通道默认写到 `logs\通道名.log`。

**某个通道启动失败，其它通道受影响吗？**
不影响。串口打不开、端口被占只会让那一个通道报错（列表里显示 `! 启动失败`），其余通道照常运行。

**中文显示乱码**
把「编码」改成 `gbk`（多数国产设备）或 `utf-8`，Telnet 客户端也需设置成对应字符集（PuTTY：Window → Translation）。

**输入没有反应 / 输入被拒绝**
默认「仅第一个客户端可写」，第二个及之后的连接是只读，会收到 `[提示] 当前连接为只读`；
想多人同时写就把写入权限改成「所有客户端都可写」（注意会互相混流）。

**看到的输出每一行都多一个空行**
关掉「串口→客户端 归一化 CRLF」，或改「客户端→串口 换行」。

**设备要 LF 结尾，但回车发的是 CR**
把「客户端→串口 换行」改成 `LF (\n)` 或 `CRLF`。

**客户端一按回车就断线**
多为 `0xFF` 转义问题，确认连接的是 **Telnet** 模式（而不是裸 TCP）；裸 TCP 模式下请用 `nc` 之类不带协商的客户端。

**想看谁连上了**
界面「已连接客户端」列出通道名、`[RW]`（可写）/ `[RO]`（只读）以及收发的字节数。

---

## 8. 自测（不需要真实串口）

```powershell
.\.venv\Scripts\python.exe tests\selftest.py    # 92 项：协商、广播、转义、只读拦截、回显、socket 后端、错误处理、
                                                #        多通道互不串台、故障隔离、配置迁移、冲突检查、命令行解析
.\.venv\Scripts\python.exe tests\gui_smoke.py   # 99 项：开窗口、点按钮、连客户端、收发、多通道同跑 + DPI/字号缩放与布局重排
```

界面截图（不同宽度、不同字号）可以这样生成，产物在 `build\shots\`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\capture_ui.ps1 -Scale 1.25
```

---

## 9. 环境说明

* Python **3.8+**（本机用 3.9.2 验证通过），Windows 10/11。
* 所有运行都在仓库内的虚拟环境 `.venv` 里，不污染系统 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # 目前无依赖，可跳过
```

* 串口访问通过 `ctypes` 直接调用 Win32（`CreateFileW` / `SetCommState` / `ReadFile` / `WriteFile`），
  因此**不需要 pyserial**；`socket://` 与 `loop://` 用于串口服务器和自测。

---

## 10. 编译成 exe

成品已经放在 `dist/`，**目标机器不需要安装 Python**（exe 内置了 Python 3.9 运行时，64 位）。

| 产物 | 大小 | 特点 |
| --- | --- | --- |
| `dist\COM-Telnet网关.exe` | 8.4 MB | 单文件图形界面，拷贝一个文件就能用；启动时先把运行时解包到临时目录（首次约 2–4 秒） |
| `dist\com-telnet-cli.exe` | 5.6 MB | 单文件命令行版，适合做服务/随系统启动 |
| `dist\onedir\COM-Telnet网关\` | 19.0 MB（整个文件夹） | 免解包**绿色版**：启动快，不受临时目录/杀软限制影响 |
| `dist\onedir\com-telnet-cli\` | 11.8 MB（整个文件夹） | 免解包命令行版 |

两个版本功能完全一致，按环境挑一个即可：

* 想「一个文件拷来拷去」→ 用单文件版；
* 机器上临时目录受限、或杀软对自解压 exe 敏感、或追求启动速度 → 用 `onedir` 绿色版（连文件夹一起拷贝）。

使用：

```
dist\COM-Telnet网关.exe                                     # 双击，图形界面
dist\com-telnet-cli.exe -p COM3 -b 115200 -t 2323           # 命令行（单通道）
dist\com-telnet-cli.exe -p COM3 -t 2323 -p COM5 -t 2324     # 命令行（多通道）
dist\com-telnet-cli.exe --map COM3=2323 --map COM5=2324     # 同上，另一种写法
dist\com-telnet-cli.exe --list-ports                        # 列出本机串口
```

`config.json` 与日志文件会生成在 **exe 所在目录**（绿色版在各自文件夹里）。

### 重新编译

```powershell
build_exe.bat
```

脚本会：确保 `.venv` 存在 → 离线安装 PyInstaller（用 `vendor/` 里已下载的 wheel）→ 生成图标 → 依次打包 4 个产物。
也可以直接右键运行 `build_exe.ps1`（可加 `-SkipIcon` 跳过图标生成）。

打包相关文件：

* `packaging/exe_gui.py`、`packaging/exe_cli.py` —— PyInstaller 入口脚本
* `packaging/make_icon.py` —— 用标准库生成 `app.ico`（7 种尺寸，`check_icon.py` 逐像素校验）
* `packaging/version_info.txt` —— exe 右键属性里的产品名/版本
* `packaging/fetch_wheels.py` —— 本机 pip 直连 PyPI 会卡住，这个脚本用 Python 自带的 HTTPS 手工抓 wheel 到 `vendor/`（`python packaging/fetch_wheels.py pyinstaller`）

> 提示：若打包后双击没反应，多半是杀软拦了自解压行为，换 `onedir` 绿色版即可；
> 想在别的机器上跑，直接拷 `dist` 里对应产物即可（单文件版一个文件，绿色版整个文件夹）。

### 用 GitHub Actions 自动编译

仓库已配置 [`.github/workflows/build-exe.yml`](.github/workflows/build-exe.yml)，推代码上去就会在 GitHub 的
Windows 机器上自动跑：

1. 装 Python 3.9 → 建 `.venv` → 装 PyInstaller（版本固定在 `PYINSTALLER_VERSION`）
2. 跑功能自测 92 项（`tests/selftest.py`，用 `loop://`，不需要串口）
3. 跑界面冒烟测试 99 项（需要桌面会话，失败不阻断打包）
4. 自动生成图标并打包出 4 个产物，还会运行 `com-telnet-cli.exe --version` 确认 exe 真能启动
5. 把产物上传成 Artifact（Actions 页面每次运行都能下载）

**打标签发版**：推一个 `v*` 标签（例如 `v1.0.0`）就会自动创建 Release 并附上 exe：

```powershell
git tag v1.0.0
git push origin v1.0.0
```

> 推代码 / 发 PR **不会**发版，只会打包并上传 Artifact；只有推 `v*` 标签才会建 Release。
>
> 建出来的是 **草稿（draft）**：只有仓库成员在 Releases 页面能看到，带 `Draft` 标记。
> 检查无误后点开该草稿右上角的 **Publish release** 才正式对外发布。
> 想改回"构建完就直接公开发布"，把工作流里 `draft: true` 删掉（或改成 `false`）即可。

发版页：<https://github.com/wdj2613/com_to_telnet/releases> —— 附件刻意用纯 ASCII 命名
（`COM-Telnet-Gateway.exe`、`com-telnet-cli.exe`、`COM-Telnet-Gateway-onedir.zip`、
`com-telnet-cli-onedir.zip`、`readme-zh.txt`），因为 **GitHub 会改写甚至丢弃非 ASCII 的附件名**
（实测 `COM-Telnet网关.exe` 被改成 `COM-Telnet.exe`、`说明.txt` 直接消失）。
重新打同一个标签（`git tag -f v1.0.0 && git push -f origin v1.0.0`）时，
工作流会先用 `GITHUB_TOKEN` 删掉该标签下的旧 Release 再重建，避免页面上留垃圾附件。

想手动触发：GitHub 仓库页面 → **Actions** → 左侧「构建 Windows exe」→ **Run workflow**。
下载路径：Actions → 某次运行 → 页面底部 **Artifacts** → `COM-Telnet-gateway-win-x64`。

在命令行里查看最近一次运行的结果和产物：

```powershell
.\.venv\Scripts\python.exe tools\check_ci.py            # 公开仓库免登录
.\.venv\Scripts\python.exe tools\check_ci.py --all      # 最近 10 次
set GITHUB_TOKEN=xxx && .\.venv\Scripts\python.exe tools\check_ci.py   # 私有仓库需要 token
```

> CI 里从 PyPI 安装 PyInstaller；本机离线时则用仓库里 `vendor/` 已存好的 wheel（`build_exe.ps1` 会自动判断）。
> 如果你的仓库默认分支不是 `main`，把徽章和工作流里的分支名改一下即可。
