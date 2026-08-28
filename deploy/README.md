# Linux 服务器部署

Windows 那套（计划任务 → `supervisor.py` → `main.py serve`）在 Linux 上塌成一层：
**systemd 直接管 `main.py serve`**。

`supervisor.py` 里手写的东西 systemd 全部原生支持：

| supervisor.py 手写的 | systemd 里对应 |
|---|---|
| 端口占位保证单例 | 一个 unit 天然只有一个实例 |
| 崩溃后退避重启（5s→…→10min） | `Restart=always` + `StartLimitIntervalSec/Burst` |
| `taskkill /T` 杀整棵树 | `KillMode=control-group` |
| 前台进程会被会话连坐 | 父进程是 PID 1，跟任何 SSH 会话无关 |
| 假死探针（真查库） | `latam-hub-health.timer` + `health-check.sh` |

> README 里那条"已知边界"（开机触发要跑在 SYSTEM 账户 / session 0，
> 那里没有桌面和 Chrome 配置，Selenium 直接废掉）**在 Linux 上不存在**。
> systemd 服务本来就是无桌面运行的，`systemctl enable` 之后重启自动起，不需要有人登录。

---

## 装

```bash
sudo bash deploy/install.sh
```

幂等，可以反复跑。它会做八件事：建服务账号 `latam` → 装系统依赖和字体 →
**装 Google Chrome** → 建目录改权限 → 生成密钥文件 → 建 venv 装依赖 →
装 Playwright chromium → `main.py init` + 注册 systemd。

### 为什么必须单独装 Chrome

`undetected-chromedriver` 找的是**系统安装的 Google Chrome**。
`playwright install chromium` 装的是 Playwright 的私有 chromium，它看不见。
全项目没有任何 `browser_executable_path` / `binary_location` 配置。

Windows 上你本机装着 Chrome 所以这个依赖一直是隐式的。服务器是干净的 ——
不装 Chrome，主引擎（Selenium）整条废掉，只剩兜底的 Playwright，
而 Liverpool 这类渠道正是 Playwright 拦死、Selenium 一次过的。

---

## 装完必做两件事

**1. 填密钥**

```bash
sudo nano /etc/latam-hub/secrets.env && sudo systemctl restart latam-hub
```

**2. 重测 29 个渠道**

```bash
sudo -u latam ./.venv/bin/python main.py doctor
```

IP 段从国内家用宽带换成了新加坡数据中心 —— 反爬表现**必然变化，方向未知**。
和 README 里 2026-08-10/08-11 那两份实测表逐条对账，哪些渠道退化了一目了然。
退化严重就在 `secrets.env` 里挂 `LATAM_SECRET_PROXY`。

---

## 日常

```bash
systemctl status latam-hub          # 状态
journalctl -u latam-hub -f          # 实时日志（替代 logs\server.log）
journalctl -t latam-hub-health -n50 # 健康探针记录（替代 logs\supervisor.log）
sudo systemctl restart latam-hub    # 重启（替代 tools\restart.ps1）
```

应用自己的日志仍写在 `logs/run-YYYYMMDD.log`，跟 Windows 上一样。

### 打开看板

服务**只监听 127.0.0.1**，没有对外暴露。这是有意的 —— 它没有任何认证，
`/api/run/collect` 能直接触发采集，密钥也在里面。别改成 `0.0.0.0`。

在**你自己电脑**上开隧道：

```bash
ssh -N -L 8765:127.0.0.1:8765 <用户>@<服务器IP>
```

然后浏览器开 `http://127.0.0.1:8765`，体验和 Windows 上双击桌面图标一样。

---

## 几个刻意的选择

**时区钉死 Asia/Shanghai**（`latam-hub.service`）。云服务器默认 UTC，
而 `runtime.yaml` 的 `daily_time: 07:30` 在 UTC 下等于北京时间 15:30 开跑 ——
这个任务要跑 10~20 小时，作息会整个错位。

**`MemoryMax=11G`**。这台机器同时跑 Claude Code。5 个 Chrome worker 内存失控时，
被 OOM-kill 的必须是本服务，不是你的开发会话。按 16G 机器分配，
剩下 5G 给 Claude Code 和系统。改并发（`parallel_workers`）时记得同步调这个值。

**不用 root 跑**。Chrome 即使有 `--no-sandbox`，以 root 跑等于把整台机器
交给被抓页面里的任意 JS —— 而爬虫恰恰是在主动加载不受信任的内容。

**密钥走环境变量**。`app/db.py` 的 `get_setting()` 优先读 `LATAM_SECRET_*`，
读不到才查 `intel.db`。这样数据库里可以一条密钥都不存，备份和传输不再有泄密风险。
Windows 上的 DPAPI 在 Linux 没有等价物，这是比明文落库更好的替代。

---

## 从 Windows 迁数据

`intel.db` 可以直接拷过来，但**密钥拷不过来** —— DPAPI 密文绑定 Windows 账号，
Linux 上解不开（这是设计使然，不是故障）。`app/db.py` 遇到这种情况会打一条
明确的 warning，而不是静默返回空。

所以：拷库 → 在 `secrets.env` 里重填密钥 → 重启。

```bash
scp data/intel.db <用户>@<服务器IP>:/path/to/项目/data/
```

存档目录（`LATAM_ARCHIVE_DIR`）和知识库（`LATAM_VAULT_DIR`）
默认在 `/var/lib/latam-hub/` 下，要迁的话一并 scp 过去。
