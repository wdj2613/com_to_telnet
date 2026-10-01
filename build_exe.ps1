# COM-Telnet 网关：一键打包 exe（全程使用 .venv 虚拟环境）
#
# 产物：
#   dist\COM-Telnet网关.exe                     单文件 GUI（拷贝即用，启动时解包到临时目录）
#   dist\com-telnet-cli.exe                     单文件命令行
#   dist\onedir\COM-Telnet网关\COM-Telnet网关.exe   免解包绿色版 GUI（启动更快，受限环境/杀软更友好）
#   dist\onedir\com-telnet-cli\com-telnet-cli.exe   免解包绿色版命令行
#
# 用法：右键“使用 PowerShell 运行”，或  build_exe.bat

[CmdletBinding()]
param([switch]$SkipIcon)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 让 PyInstaller 的缓存落在工作区内（避免写到用户目录被安全策略拦住）
$env:PYINSTALLER_CONFIG_DIR = Join-Path $Root 'build\pyi-config'

$Py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { throw '未找到 .venv，请先双击 run.bat 创建虚拟环境' }

& $Py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[setup] 离线安装 PyInstaller（使用 vendor\ 里已下载的 wheel）...'
    & $Py -m pip install --no-index --find-links (Join-Path $Root 'vendor') pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败' }
}

if (-not $SkipIcon) {
    Write-Host '[setup] 生成图标 packaging\app.ico ...'
    & $Py (Join-Path $Root 'packaging\make_icon.py')
    if ($LASTEXITCODE -ne 0) { throw '图标生成失败' }
}

$icon = Join-Path $Root 'packaging\app.ico'
$verFile = Join-Path $Root 'packaging\version_info.txt'
$specPath = Join-Path $Root 'build'
$common = @(
    '--noconfirm', '--clean',
    '--paths', $Root,
    '--icon', $icon,
    '--version-file', $verFile,
    '--specpath', $specPath
)

function Invoke-Build {
    param(
        [string]$Name,
        [string]$Entry,
        [string]$Mode,      # onefile | onedir
        [string]$Window,    # windowed | console
        [string]$Dist,
        [string]$Work
    )
    Write-Host ("[build] {0}  ({1} / {2})" -f $Name, $Mode, $Window)
    & $Py -m PyInstaller @common "--$Mode" "--$Window" --name $Name --distpath $Dist --workpath $Work (Join-Path $Root $Entry)
    if ($LASTEXITCODE -ne 0) { throw "$Name 打包失败（$Mode/$Window）" }
}

$dist = Join-Path $Root 'dist'
$distOne = Join-Path $Root 'dist\onedir'

# 先清掉旧产物；如果 exe 还在运行会删不掉，直接给出能看懂的提示
foreach ($f in @((Join-Path $dist 'COM-Telnet网关.exe'), (Join-Path $dist 'com-telnet-cli.exe'))) {
    if (Test-Path $f) {
        try {
            Remove-Item $f -Force -ErrorAction Stop
        } catch {
            throw "无法覆盖 $f —— 请先关闭正在运行的 exe（任务管理器里结束同名进程）后重试"
        }
    }
}

Invoke-Build -Name 'COM-Telnet网关' -Entry 'packaging\exe_gui.py' -Mode onefile -Window windowed -Dist $dist   -Work (Join-Path $Root 'build\onefile-gui')
Invoke-Build -Name 'com-telnet-cli' -Entry 'packaging\exe_cli.py' -Mode onefile -Window console  -Dist $dist   -Work (Join-Path $Root 'build\onefile-cli')
Invoke-Build -Name 'COM-Telnet网关' -Entry 'packaging\exe_gui.py' -Mode onedir  -Window windowed -Dist $distOne -Work (Join-Path $Root 'build\onedir-gui')
Invoke-Build -Name 'com-telnet-cli' -Entry 'packaging\exe_cli.py' -Mode onedir  -Window console  -Dist $distOne -Work (Join-Path $Root 'build\onedir-cli')

# 中文说明随产物一起发出去（内容放在 packaging\readme-release.txt，脚本里不塞长文本）
Copy-Item (Join-Path $Root 'packaging\readme-release.txt') (Join-Path $dist '说明.txt') -Force

Write-Host ''
Write-Host '打包完成，产物如下：' -ForegroundColor Green
Get-ChildItem $dist -File | ForEach-Object { Write-Host ('  {0,-24} {1,7:N2} MB' -f $_.Name, ($_.Length / 1MB)) }
Get-ChildItem $distOne -Directory | ForEach-Object {
    $sum = (Get-ChildItem $_.FullName -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-Host ('  {0,-24} {1,7:N2} MB  （整个文件夹一起拷贝）' -f $_.Name, ($sum / 1MB))
}
Write-Host ''
Write-Host '直接双击 dist\COM-Telnet网关.exe 即可使用。'
