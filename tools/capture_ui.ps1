# 启动源码版界面，按不同窗口宽度截图，用于人工核对自适应效果。
# 用法： powershell -NoProfile -ExecutionPolicy Bypass -File tools\capture_ui.ps1 [-Scale 1.25]
param(
    [string]$OutDir = "",
    [double]$Scale = 0.0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $OutDir) { $OutDir = Join-Path $Root "build\shots" }
New-Item -ItemType Directory -Force $OutDir | Out-Null

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class UiWin {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int t, bool repaint);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
"@

function Save-Shot {
    param([IntPtr]$Handle, [string]$Path)
    $rect = New-Object UiWin+RECT
    [void][UiWin]::GetWindowRect($Handle, [ref]$rect)
    $w = $rect.Right - $rect.Left
    $h = $rect.Bottom - $rect.Top
    $bmp = New-Object System.Drawing.Bitmap($w, $h)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bmp.Size)
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose()
    $bmp.Dispose()
    Write-Host ("  截图 {0}  ({1}x{2})" -f (Split-Path $Path -Leaf), $w, $h)
}

function Get-BridgeProcess {
    for ($i = 0; $i -lt 40; $i++) {
        $p = Get-Process | Where-Object { $_.MainWindowTitle -like '*COM-Telnet*' -and $_.MainWindowHandle -ne 0 } | Select-Object -First 1
        if ($p) { return $p }
        Start-Sleep -Milliseconds 250
    }
    throw "没有找到界面窗口"
}

function Start-Bridge {
    param([double]$Zoom, [int]$WinW, [int]$WinH)
    $cfg = Join-Path $Root "config.json"
    $json = [ordered]@{
        port = ""; baudrate = 115200; listen_port = 2323; listen_host = "0.0.0.0"
        banner = "欢迎使用 COM-Telnet 网关"
        ui_scale = $Zoom
        window_geometry = "$WinW`x$WinH"
        timestamps = $true; show_rx = $true; show_tx = $true
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText($cfg, $json, (New-Object System.Text.UTF8Encoding($false)))
    $py = Join-Path $Root ".venv\Scripts\pythonw.exe"
    $proc = Start-Process -FilePath $py -ArgumentList "`"$(Join-Path $Root 'com_telnet_bridge.py')`"" `
        -WorkingDirectory $Root `
        -RedirectStandardOutput (Join-Path $OutDir "gui.out") `
        -RedirectStandardError (Join-Path $OutDir "gui.err") -PassThru
    Start-Sleep -Seconds 3
    return $proc
}

if ($Scale -le 0) {
    # 没传就按 100% 处理（本机是 125%，调用时传 -Scale 1.25）
    $Scale = 1.0
}
Write-Host ("屏幕缩放系数：{0}" -f $Scale)

$proc = Start-Bridge -Zoom 1.0 -WinW ([int](1180 * $Scale)) -WinH ([int](720 * $Scale))
$win = Get-BridgeProcess
[void][UiWin]::SetForegroundWindow($win.MainWindowHandle)

# 按逻辑宽度换算成物理像素，依次截图
$shots = @(
    @{ w = 1180; h = 720; name = "01-wide-1180" },
    @{ w = 980;  h = 700; name = "02-medium-980" },
    @{ w = 660;  h = 760; name = "03-narrow-660" }
)
foreach ($shot in $shots) {
    [void][UiWin]::MoveWindow($win.MainWindowHandle, 40, 30, [int]($shot.w * $Scale), [int]($shot.h * $Scale), $true)
    Start-Sleep -Milliseconds 1000
    Save-Shot -Handle $win.MainWindowHandle -Path (Join-Path $OutDir "$($shot.name).png")
}

Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Get-Process | Where-Object { $_.MainWindowTitle -like '*COM-Telnet*' } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# 界面字号放大到 140% 的对照图
$proc = Start-Bridge -Zoom 1.4 -WinW ([int](1180 * $Scale)) -WinH ([int](760 * $Scale))
$win = Get-BridgeProcess
[void][UiWin]::SetForegroundWindow($win.MainWindowHandle)
[void][UiWin]::MoveWindow($win.MainWindowHandle, 40, 30, [int](1180 * $Scale), [int](760 * $Scale), $true)
Start-Sleep -Milliseconds 1200
Save-Shot -Handle $win.MainWindowHandle -Path (Join-Path $OutDir "04-zoom-140.png")

Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Get-Process | Where-Object { $_.MainWindowTitle -like '*COM-Telnet*' } | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root "config.json") -Force -ErrorAction SilentlyContinue
Write-Host "完成"
