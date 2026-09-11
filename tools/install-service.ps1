# 安装/卸载「拉美竞品情报中枢」的开机自启与桌面图标。
# 中文 .ps1 必须存成 UTF-8 BOM —— 本机 ANSI 是 cp936，无 BOM 会读成乱码。
# 兼容 Windows PowerShell 5.1：不用 && 、不用三元、不用 ?? 。
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$Root     = Split-Path -Parent $PSScriptRoot
$TaskName = "LatamIntelHub"
$PyW      = "C:\Python314\pythonw.exe"
$Sup      = Join-Path $Root "tools\supervisor.py"
$Launcher = Join-Path $Root "tools\open-dashboard.cmd"
$Icon     = Join-Path $Root "assets\hub.ico"
$Desktop  = [Environment]::GetFolderPath("Desktop")
$Link     = Join-Path $Desktop "拉美竞品情报中枢.lnk"

function Say($m) { Write-Host "  $m" }

# --------------------------- 卸载 ---------------------------
if ($Uninstall) {
  Write-Host ""
  Write-Host "正在卸载..." -ForegroundColor Yellow
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($t) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Say "已删除计划任务 $TaskName"
  } else {
    Say "计划任务不存在，跳过"
  }
  if (Test-Path $Link) { Remove-Item $Link -Force; Say "已删除桌面图标" }
  Write-Host ""
  Write-Host "卸载完成。正在跑的守护进程不受影响，重启电脑后就不会再自启了。" -ForegroundColor Green
  Write-Host ""
  exit 0
}

# ------------------------ 安装前自检 ------------------------
Write-Host ""
Write-Host "拉美竞品情报中枢 -- 安装开机自启" -ForegroundColor Cyan
Write-Host "==================================="
Write-Host ""

if (-not (Test-Path $PyW))      { throw "找不到 pythonw.exe：$PyW" }
if (-not (Test-Path $Sup))      { throw "找不到守护进程脚本：$Sup" }
if (-not (Test-Path $Launcher)) { throw "找不到启动器：$Launcher" }
$LauncherPy = Join-Path $Root "tools\launcher.py"
if (-not (Test-Path $LauncherPy)) { throw "找不到无窗口启动器：$LauncherPy" }
Say "依赖文件齐全"

# ------------------------ 注册计划任务 ------------------------
# 触发器用「登录时」而不是「开机时」，这是有意的：
#   开机触发要在 SYSTEM 账户下跑（session 0），那里没有用户桌面、
#   没有用户的 Chrome 配置，Selenium/undetected-chromedriver 直接废掉。
#   而且注册 SYSTEM 任务需要管理员权限，当前账户没有。
#   登录触发跑在你自己的会话里，Chrome 能正常用。
# 延迟 40 秒：等网络栈和杀软扫描过去，避免开机瞬间抢资源。

$action = New-ScheduledTaskAction -Execute $PyW `
          -Argument ('"{0}"' -f $Sup) -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT40S"

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -DontStopOnIdleEnd `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 -RestartInterval ([TimeSpan]::FromMinutes(1)) `
  -MultipleInstances IgnoreNew

$settings.Hidden = $true

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
             -LogonType Interactive -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Say "已移除旧的同名任务"
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal `
  -Description "拉美竞品情报中枢：登录后自动拉起守护进程，服务掉线自动重启并报警" | Out-Null
Say "计划任务已注册：$TaskName（登录后 40 秒启动，掉线自动重启）"

# ------------------------ 桌面图标 ------------------------
# ★ 图标直接指向 pythonw.exe，不经过 .cmd。
#   .cmd 一定会创建控制台窗口 —— 即使快捷方式设成"最小化"，它仍然会在
#   任务栏闪一下、抢一次焦点，出错时还会把 traceback 糊在黑窗口里。
#   pythonw.exe 是 GUI 子系统程序，根本不分配控制台，全程零窗口；
#   出错时由 launcher.py 弹原生消息框。
$sh = New-Object -ComObject WScript.Shell
$sc = $sh.CreateShortcut($Link)
$sc.TargetPath       = $PyW
$sc.Arguments        = ('"{0}"' -f (Join-Path $Root "tools\launcher.py"))
$sc.WorkingDirectory = $Root
$sc.Description      = "打开拉美竞品情报中枢看板（没启动会自动拉起，无窗口）"
$sc.WindowStyle      = 1
if (Test-Path $Icon) { $sc.IconLocation = $Icon }
$sc.Save()
Say "桌面图标已创建：$Link"

# ------------------------ 立刻起一次 ------------------------
Write-Host ""
Say "正在启动服务（不用等下次登录）..."
Start-ScheduledTask -TaskName $TaskName

$ok = $false
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Seconds 3
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8765/api/health" `
         -TimeoutSec 5 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { }
}

Write-Host ""
if ($ok) {
  Write-Host "[OK] 装好了，服务已经在跑。" -ForegroundColor Green
  Write-Host ""
  Say "看板地址   http://127.0.0.1:8765"
  Say "桌面图标   拉美竞品情报中枢"
  Say "开机       登录后 40 秒自动启动，无窗口"
  Say "掉线       守护进程 60 秒探一次，连续 3 次失败就重启并发 Telegram"
  Say "日志       logs\supervisor.log（守护） / logs\server.log（服务）"
  Write-Host ""
  Say "卸载：powershell -ExecutionPolicy Bypass -File tools\install-service.ps1 -Uninstall"
} else {
  Write-Host "[!] 计划任务装好了，但服务 120 秒内没答应。" -ForegroundColor Yellow
  Write-Host ""
  Say "去看 logs\server.log 和 logs\supervisor.log"
  Say "多半是依赖没装全 -- 先跑一次 1-install.bat"
}
Write-Host ""
