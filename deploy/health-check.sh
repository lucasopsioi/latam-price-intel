#!/usr/bin/env bash
# ============================================================================
#  假死检测 —— 对应 tools/supervisor.py 里那个"真的查一次库"的探针。
#
#  systemd 的 Restart=always 只管进程**死了**的情况。但你在 Windows 上踩过的
#  是另一种：端口通、进程活着，库锁死 / 磁盘满 / schema 没迁移，界面全白。
#  这个脚本补的就是这一段。
#
#  三条纪律照搬 README：
#    1. 查 /api/health（它真的会 SELECT 一次），不是看端口通不通
#    2. **连续 3 次**失败才重启 —— 采集正忙时响应会慢，一次超时就重启是误杀
#    3. 外网断了只报警**不重启** —— 重启治不好断网，只会打断正在跑的采集
# ============================================================================
set -uo pipefail

URL="http://127.0.0.1:8765/api/health"
UNIT="latam-hub.service"
STATE_DIR="/run/latam-hub"
FAIL_FILE="$STATE_DIR/health-fails"
MAX_FAILS=3

mkdir -p "$STATE_DIR"
fails="$(cat "$FAIL_FILE" 2>/dev/null || echo 0)"
case "$fails" in ''|*[!0-9]*) fails=0 ;; esac    # 文件被写坏时从 0 重来

body="$(curl -fsS --max-time 20 "$URL" 2>/dev/null || true)"

if printf '%s' "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
  if [ "$fails" -gt 0 ]; then
    # ★ 恢复也要报一条。只报故障不报恢复，你永远不知道现在到底好没好。
    logger -t latam-hub-health "健康已恢复（此前连续失败 $fails 次）"
  fi
  echo 0 > "$FAIL_FILE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$FAIL_FILE"
logger -t latam-hub-health "健康检查失败 $fails/$MAX_FAILS（响应：${body:-空}）"

[ "$fails" -ge "$MAX_FAILS" ] || exit 0

# 到阈值了，重启前先分清是"服务坏了"还是"外网断了"。
# 两个独立目标都不通才判定断网 —— 单一目标挂掉不算。
if ! curl -fsS --max-time 15 -o /dev/null https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null \
   && ! curl -fsS --max-time 15 -o /dev/null https://api.github.com 2>/dev/null; then
  logger -t latam-hub-health "外网不可达 → 判定为断网，不重启（重启治不好断网）。采集会空转，请检查网络。"
  exit 0
fi

logger -t latam-hub-health "连续 $fails 次失败且外网正常 → 重启 $UNIT"
echo 0 > "$FAIL_FILE"
systemctl restart "$UNIT"
