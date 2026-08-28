# -*- coding: utf-8 -*-
"""过一次人机验证 / 登录，把会话存下来给采集复用。

用法：
    python tools\\site_login.py fastshop
    python tools\\site_login.py ripley_pe
    python tools\\site_login.py --list

★ 为什么是这个设计：
  有些站点（Fast Shop、Ripley 秘鲁）会弹人机验证。**验证本身必须由你本人完成** ——
  这个工具只负责：打开一个可见的浏览器、停在那里等你操作、你说完成后把
  Cookie 存进 data/browser_profiles/，之后采集自动带上。

  工具不读取、不保存、也不代填任何账号密码。它连输入框都不碰，
  你在浏览器里做的一切它都不看，只在你按回车后导出 Cookie。

  验证通过后的会话通常能用几天到几周。到期了报告里会重新出现拦截警告，
  那时再跑一次即可。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402
from app.scraping.browser import COUNTRY_PROFILE, DEVICE_PROFILES, _DEFAULT_PROFILE  # noqa: E402
from app.scraping.selenium_driver import chrome_major_version  # noqa: E402


def _targets() -> dict:
    """从渠道配置里取可登录的站点。"""
    db.init_db()
    out = {}
    for c in db.q("SELECT code, name, country_code, base_url FROM channel "
                  "WHERE base_url IS NOT NULL AND base_url <> '' ORDER BY country_code"):
        key = f"{c['code']}_{c['country_code'].lower()}"
        out[key] = c
        out.setdefault(c["code"], c)       # 短名也能用（同名取第一个）
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    targets = _targets()

    if not args or args[0] in ("--list", "-l", "list"):
        print("可用站点（传 code 或 code_国家）：\n")
        seen = set()
        for k, c in sorted(targets.items()):
            if c["code"] in seen and "_" not in k:
                continue
            if "_" in k:
                seen.add(c["code"])
                print(f"  {k:<20} {c['country_code']} {c['name']}")
        print("\n例：python tools\\site_login.py fastshop_br")
        return 0

    key = args[0].lower()
    ch = targets.get(key)
    if not ch:
        print(f"找不到站点「{key}」。用 --list 看可用列表。")
        return 1

    cc = ch["country_code"]
    prof = COUNTRY_PROFILE.get(cc, _DEFAULT_PROFILE)
    profile_dir = config.PROFILE_DIR / f"selenium_{cc}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 66}")
    print(f"  站点：{ch['name']}（{cc}）")
    print(f"  地址：{ch['base_url']}")
    print(f"{'=' * 66}\n")
    print("  即将打开一个浏览器窗口。请你本人在里面：")
    print("    · 完成人机验证（勾选/点选图片等）")
    print("    · 如果该站需要登录才能看价格，用你自己的账号登录")
    print("    · 确认能正常看到商品列表")
    print("\n  ★ 本工具不读取也不保存你的账号密码，只在你确认后导出 Cookie。\n")
    input("  准备好了按回车打开浏览器…")

    try:
        import undetected_chromedriver as uc
    except ImportError:
        print("  未安装 undetected-chromedriver，请先跑 1-install.bat")
        return 1

    opts = uc.ChromeOptions()
    # 有头 + 正常桌面尺寸：人机验证在手机模拟下有时点不动
    opts.add_argument(f"--lang={prof['locale']}")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--disable-blink-features=AutomationControlled")

    kw = {"options": opts, "headless": False, "use_subprocess": True}
    major = chrome_major_version()
    if major:
        kw["version_main"] = major

    driver = uc.Chrome(**kw)
    try:
        driver.get(ch["base_url"])
        print("\n  浏览器已打开。完成验证/登录后回到这里。")
        input("  确认已经能正常看到网站内容了，按回车保存会话…")

        cookies = driver.get_cookies()
        # 只存 Cookie，不存任何表单内容、不截图、不读页面文本
        out = config.PROFILE_DIR / f"{ch['code']}_{cc}_cookies.json"
        out.write_text(json.dumps({
            "channel": ch["code"], "country": cc,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cookies": cookies,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

        names = sorted({c.get("name", "") for c in cookies})[:8]
        print(f"\n  ✓ 已保存 {len(cookies)} 个 Cookie → {out.name}")
        print(f"    （字段名样例：{', '.join(names)}）")
        print(f"    Chrome 资料目录也已保留：{profile_dir.name}")
        print("\n  之后的采集会自动带上这个会话。")
        print("  过期后报告里会重新出现拦截警告，那时再跑一次本工具即可。\n")
        return 0
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
