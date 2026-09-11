#!/usr/bin/env bash
# 等采集自然结束 -> 用 restart.ps1 重启 -> 验在线表格三个取数端点。
#
# 为什么要等：restart.ps1 在采集期间会拒绝（无 -Force），这是硬规矩 ——
# 采集中途重启会把整轮数据丢掉。这里只在 task_running=false 时才动手。
# 上一版窗口 13 小时不够（正常一轮 17~19 小时），这版给 26 小时。
set -u
cd "$(dirname "$0")/.." || exit 1
API=http://127.0.0.1:8765

for i in $(seq 1 390); do
  busy=$(curl -s --max-time 8 "$API/api/health" \
         | grep -o '"task_running":[a-z]*' | cut -d: -f2)
  if [ "$busy" = "false" ]; then
    echo "[$(date +%m-%d\ %H:%M)] 第 $i 次探测：采集已结束，开始重启"
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/restart.ps1 2>&1 | tail -3
    sleep 20
    echo "=== health ==="
    curl -s --max-time 15 "$API/api/health"; echo
    echo "=== 在线表格取数端点 ==="
    for k in product sku pricing; do
      printf "  %-8s " "$k"
      curl -s -w " [HTTP %{http_code}]" --max-time 15 "$API/api/products/grid/$k" \
        | PYTHONUTF8=1 "/c/Users/demo/AppData/Local/Python/pythoncore-3.14-64/python.exe" -c "
import sys, json
raw = sys.stdin.read()
code = raw.rsplit('[HTTP', 1)[-1].strip(' ]') if '[HTTP' in raw else '?'
body = raw.rsplit('[HTTP', 1)[0]
try:
    d = json.loads(body)
    print('HTTP %s  %d 行，可编辑 %d 列' % (code, len(d['rows']), len(d['editable'])))
except Exception:
    print('HTTP %s  取数失败：%s' % (code, body[:120]))
" 2>&1 | grep -v Warning
    done
    exit 0
  fi
  # 每小时报一次还在等，免得看不出死活
  [ $((i % 15)) -eq 0 ] && echo "[$(date +%m-%d\ %H:%M)] 第 $i 次探测：采集仍在跑，继续等"
  sleep 240
done
echo "[$(date +%m-%d\ %H:%M)] 26 小时内采集未结束 —— 这次要当异常查，不是等得不够"
