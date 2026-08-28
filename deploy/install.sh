#!/usr/bin/env bash
# ============================================================================
#  拉美竞品情报中枢 —— Linux 服务器部署
#
#  对应 Windows 上的 1-install.bat + tools/install-service.ps1，
#  但少了一整层：systemd 取代了 计划任务 + supervisor.py 的三层结构。
#
#  用法（在项目根目录）：
#      sudo bash deploy/install.sh
#
#  幂等：可以反复跑。已装的包会跳过，已存在的 secrets.env 不会被覆盖。
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_USER="latam"
ETC_DIR="/etc/latam-hub"
VAR_DIR="/var/lib/latam-hub"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[错误] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "需要 root：sudo bash deploy/install.sh"
[ -f "$ROOT/main.py" ] || die "在 $ROOT 找不到 main.py —— 请在项目根目录运行"

# ---------------------------------------------------------------- 1. 服务账号
# ★ 不用 root 跑。Chrome 即使加了 --no-sandbox，以 root 跑仍然是把整台机器
#   暴露给被抓页面里的任意 JS —— 爬虫恰恰是在主动加载不受信任的内容。
say "1/8 服务账号 $SVC_USER"
if id "$SVC_USER" &>/dev/null; then
  echo "    已存在，跳过"
else
  useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"
  echo "    已创建"
fi

# ---------------------------------------------------------------- 2. 系统依赖
say "2/8 系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-dev build-essential \
  curl gnupg ca-certificates tzdata \
  fonts-liberation fonts-noto-cjk fonts-noto-color-emoji
#  ↑ 字体不是可选项：headless Chrome 缺字体时文本渲染成方块，
#    虽然从 DOM 取价格不受影响，但 --show-browser 截图排障时你会什么都看不出来。

# ---------------------------------------------------------------- 3. Chrome
# ★ 这一步在 Windows 上从来不需要，因为你本机装着 Chrome。
#   服务器是干净的，而 undetected-chromedriver 找的是**系统装的 Chrome**——
#   Playwright 自带的那个私有 chromium 它看不见。
#   不装 = 主引擎（Selenium）整条废掉，只剩兜底的 Playwright。
say "3/8 Google Chrome"
if command -v google-chrome >/dev/null 2>&1; then
  echo "    已安装：$(google-chrome --version)"
else
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update -qq
  apt-get install -y -qq google-chrome-stable
  echo "    已安装：$(google-chrome --version)"
fi

# ---------------------------------------------------------------- 4. 目录
say "4/8 目录与权限"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$VAR_DIR/archive" "$VAR_DIR/vault"
install -d -m 0750 "$ETC_DIR"
chown -R "$SVC_USER:$SVC_USER" "$ROOT"
# data/ 里有 intel.db（存着密钥）和浏览器 profile，只给服务账号自己看
chmod 0700 "$ROOT/data"
echo "    项目目录 $ROOT 已归属 $SVC_USER"

# ---------------------------------------------------------------- 5. 密钥文件
say "5/8 密钥文件 $ETC_DIR/secrets.env"
if [ -f "$ETC_DIR/secrets.env" ]; then
  echo "    已存在，不覆盖"
else
  install -m 0600 -o root -g root "$ROOT/deploy/secrets.env.example" "$ETC_DIR/secrets.env"
  warn "已从模板创建，**里面还是占位值** —— 装完记得编辑："
  warn "    sudo nano $ETC_DIR/secrets.env && sudo systemctl restart latam-hub"
fi

# ---------------------------------------------------------------- 6. venv
say "6/8 Python 虚拟环境"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  sudo -u "$SVC_USER" python3 -m venv "$ROOT/.venv"
fi
sudo -u "$SVC_USER" "$ROOT/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SVC_USER" "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
echo "    $(sudo -u "$SVC_USER" "$ROOT/.venv/bin/python" --version)"

# ---------------------------------------------------------------- 7. Playwright
# 兜底引擎。--with-deps 会把 chromium 需要的系统库一并装上。
say "7/8 Playwright chromium（兜底引擎，约 150MB）"
sudo -u "$SVC_USER" "$ROOT/.venv/bin/playwright" install chromium
"$ROOT/.venv/bin/playwright" install-deps chromium >/dev/null 2>&1 || \
  warn "install-deps 失败（多数情况下 Chrome 的依赖已经覆盖了，先继续）"

# ---------------------------------------------------------------- 8. 建库 + 服务
say "8/8 建库与 systemd 服务"
sudo -u "$SVC_USER" env PYTHONUTF8=1 \
  LATAM_ARCHIVE_DIR="$VAR_DIR/archive" LATAM_VAULT_DIR="$VAR_DIR/vault" \
  "$ROOT/.venv/bin/python" "$ROOT/main.py" init

# unit 里的 ROOT 路径按实际安装位置替换掉
for unit in latam-hub.service latam-hub-health.service latam-hub-health.timer; do
  sed -e "s|@ROOT@|$ROOT|g" -e "s|@USER@|$SVC_USER|g" -e "s|@VAR@|$VAR_DIR|g" \
      -e "s|@ETC@|$ETC_DIR|g" \
      "$ROOT/deploy/$unit" > "/etc/systemd/system/$unit"
done
install -m 0755 "$ROOT/deploy/health-check.sh" /usr/local/bin/latam-hub-health

systemctl daemon-reload
systemctl enable --now latam-hub.service
systemctl enable --now latam-hub-health.timer

cat <<EOF

────────────────────────────────────────────────────────────────
 安装完成。

 下一步（必做）：
   1. 填密钥      sudo nano $ETC_DIR/secrets.env
                  sudo systemctl restart latam-hub
   2. 重测渠道    sudo -u $SVC_USER $ROOT/.venv/bin/python $ROOT/main.py doctor
                  ← 换了 IP 段，29 个渠道的可达性必须重新对账

 日常：
   看状态         systemctl status latam-hub
   看日志         journalctl -u latam-hub -f
   重启           sudo systemctl restart latam-hub
   打开看板       在**你自己电脑**上跑：
                  ssh -N -L 8765:127.0.0.1:8765 <用户>@<服务器IP>
                  然后浏览器开 http://127.0.0.1:8765

 服务只监听 127.0.0.1，没有对外暴露 —— 这是有意的，它没有任何认证。
────────────────────────────────────────────────────────────────
EOF
