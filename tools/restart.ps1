# 可靠地重启情报中枢。中文 .ps1 必须 UTF-8 BOM（本机 ANSI 是 cp936）。
#
# ★★ 为什么不能直接用 `Restart-ScheduledTask`：实测它**什么也没做** ——
#   守护进程和服务进程的 PID 一个都没变，supervisor.log 里连一条新的启动记录
#   都没有。原因是任务设了 MultipleInstances=IgnoreNew：Stop 是异步的，
#   紧跟着的 Start 在旧实例还没退干净时被直接忽略，于是"重启"变成空操作。
#   最坑的是它**不报错**，你以为重启了，其实跑的还是旧代码。
#
# 正确顺序：停任务 → 按 PID 杀掉守护与服务（杀树）→ 等端口释放 → 起任务 → 等健康。
param([int]$Port = 8765, [int]$TimeoutSec = 150, [switch]$Force)

$ErrorActionPreference = "Stop"
$TaskName = "LatamIntelHub"

function Say($m) { Write-Host "  $m" }

Write-Host ""
Write-Host "重启拉美竞品情报中枢" -ForegroundColor Cyan
Write-Host "======================"

# ★★ 采集保护：正在采集时重启会把整轮杀掉（Chrome 会话全断、轮次判中断）。
#   实测 8/17、8/19 两天的每日采集（12:30 起跑的 #37/#40）都被手动重启打断，
#   一条数据没落。铁律「采集期间不要重启服务」写在记忆里没用 —— 要写在脚本里。
#   真要立刻重启（明知会牺牲本轮）加 -Force。
try {
  $t = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/task" -TimeoutSec 5 -UseBasicParsing
  $tj = $t.Content | ConvertFrom-Json
  if ($tj.running -and -not $Force) {
    Write-Host ""
    Write-Host ("[X] 拒绝重启：任务「" + $tj.name + "」正在运行 —— " + $tj.progress) -ForegroundColor Red
    Say "重启会把整轮采集杀掉（上两次每日采集就是这么没的）。"
    Say "等它跑完再重启；或明确要牺牲本轮：加 -Force"
    Write-Host ""
    exit 2
  }
} catch { }

Say "1/4 停止计划任务"
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { Say "（任务未在运行）" }

Say "2/4 结束残留进程"
# ★ 只按 PID + /T 杀树，绝不按映像名 —— 按名字会误伤你自己开的 Chrome / Python
#
# ★★ 两个 PS5.1 的坑，都踩过：
#   ① 不能写 `taskkill ... 2>&1`：PS5.1 会把原生命令的 stderr 包成
#      NativeCommandError，配合 $ErrorActionPreference='Stop' 直接中止脚本。
#   ② 上一步 Stop-ScheduledTask 可能已经把守护干掉了，于是 taskkill 报
#      "没有找到进程 41276" —— 这是**正常情况**不是故障。
#   所以：先确认进程还在，再杀；并且把这一段的错误降级为不致命。
$n = 0
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $targets = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like 'python*' -and
    ($_.CommandLine -like '*supervisor.py*' -or $_.CommandLine -like '*main.py serve*')
  })
  foreach ($t in $targets) {
    if (-not (Get-Process -Id $t.ProcessId -ErrorAction SilentlyContinue)) { continue }
    & taskkill /PID $t.ProcessId /T /F > $null 2> $null
    $n++
  }
} finally {
  $ErrorActionPreference = $prev
}
Say "已结束 $n 个进程"

# 等端口真正释放。★ 进程没了不等于端口立刻可用（TIME_WAIT），
#   不等就起会让新服务因端口占用而启动失败。
for ($i = 0; $i -lt 20; $i++) {
  $held = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if (-not $held) { break }
  Start-Sleep -Milliseconds 500
}

Say "3/4 启动计划任务"
Start-ScheduledTask -TaskName $TaskName

Say "4/4 等待服务就绪"
$ok = $false
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 3
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 5 -UseBasicParsing
    if ($r.StatusCode -eq 200) {
      $j = $r.Content | ConvertFrom-Json
      # uptime 小才说明是**新**进程；不判这个会把旧进程当成"重启成功"
      if ($j.uptime_sec -lt 90) { $ok = $true; break }
    }
  } catch { }
}

Write-Host ""
if ($ok) {
  Write-Host "[OK] 已重启，服务在跑。" -ForegroundColor Green
  Say "看板 http://127.0.0.1:$Port"
} else {
  Write-Host "[!] $TimeoutSec 秒内没等到新服务。" -ForegroundColor Yellow
  Say "看 logs\server.log 与 logs\supervisor.log"
}
Write-Host ""
