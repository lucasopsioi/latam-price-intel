# 启动失败时弹给用户看的窗口。中文 .ps1 必须存成 UTF-8 BOM，否则本机 cp936 会读乱。
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$root = Split-Path -Parent $PSScriptRoot
$log  = Join-Path $root "logs\server.log"
$sup  = Join-Path $root "logs\supervisor.log"

$tail = ""
if (Test-Path $log) {
  $tail = (Get-Content $log -Tail 12 -ErrorAction SilentlyContinue) -join "`n"
}

$msg = @"
情报中枢没能启动起来（等了 90 秒仍无响应）。

常见原因：
  1. 端口 8765 被别的程序占了
  2. 依赖没装全 —— 先跑一次 1-install.bat
  3. 数据库被别的进程锁住了

最近的服务日志：
$tail

完整日志：
$log
$sup
"@

[System.Windows.Forms.MessageBox]::Show(
  $msg, "拉美竞品情报中枢 —— 启动失败",
  [System.Windows.Forms.MessageBoxButtons]::OK,
  [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
