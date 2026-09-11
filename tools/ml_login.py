# -*- coding: utf-8 -*-
"""MercadoLibre 登录态保存工具。

用法：  python tools\ml_login.py MX

会打开一个可见的浏览器窗口，**由你本人在里面登录**。登录完回命令行按回车，
会话 Cookie 存到 data/browser_profiles/<国家>.json，之后采集自动复用。

★ 边界（不可越）：
  本工具不读取、不保存、不传输你的账号密码 —— 只把浏览器登录后产生的
  会话 Cookie 导出到本地文件。密码全程只经过你和 MercadoLibre 之间。
  建议用一个可弃的普通买家账号，不要用主账号。

为什么需要这一步：
  实测 ML 对未登录访客的搜索页/商品页强制 302 到 /gz/account-verification，
  headless / 有头 / 换设备全部撞墙 —— 这是访客策略，不是指纹问题。
  更省事的做法是去 developers.mercadolibre.com 免费注册拿 access token
  （官方授权通道，零封禁风险），填进设置页即可，不必走这一步。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.scraping.browser import COUNTRY_PROFILE, DEVICE_PROFILES, _DEFAULT_PROFILE  # noqa: E402

SITES = {
    "MX": "https://www.mercadolibre.com.mx",
    "BR": "https://www.mercadolivre.com.br",
    "CO": "https://www.mercadolibre.com.co",
    "CL": "https://www.mercadolibre.cl",
    "PE": "https://www.mercadolibre.com.pe",
    "AR": "https://www.mercadolibre.com.ar",
}


def main() -> int:
    cc = (sys.argv[1] if len(sys.argv) > 1 else "MX").upper()
    if cc not in SITES:
        print(f"不支持的国家：{cc}。可选：{', '.join(SITES)}")
        return 1

    db.init_db()
    from playwright.sync_api import sync_playwright

    prof = COUNTRY_PROFILE.get(cc, _DEFAULT_PROFILE)
    dev = DEVICE_PROFILES["galaxy_s24"]
    state_file = config.PROFILE_DIR / f"{cc}.json"

    print("=" * 66)
    print(f"MercadoLibre 登录态保存 —— {cc}")
    print("=" * 66)
    print("即将打开浏览器窗口。请你**本人**在窗口里完成登录。")
    print("建议使用可弃的普通买家账号，不要用主账号。")
    print("本工具不读取、不保存你的账号密码，只导出登录后的会话 Cookie。")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False,
                                    args=["--disable-blink-features=AutomationControlled"])
        kw = dict(user_agent=dev["user_agent"], viewport=dev["viewport"],
                  device_scale_factor=dev["device_scale_factor"],
                  is_mobile=True, has_touch=True,
                  locale=prof["locale"], timezone_id=prof["tz"],
                  extra_http_headers={"Accept-Language": prof["al"]})
        if state_file.exists():
            kw["storage_state"] = str(state_file)
            print(f"（检测到已有登录态 {state_file.name}，将在其基础上续期）\n")
        ctx = browser.new_context(**kw)
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        page.goto(SITES[cc], wait_until="domcontentloaded")

        print(f"已打开 {SITES[cc]}")
        print("→ 点右上角登录，完成后回到这里按【回车】。")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            browser.close()
            return 1

        ctx.storage_state(path=str(state_file))
        n_cookies = len(ctx.cookies())
        browser.close()

    print(f"\n登录态已保存：{state_file}")
    print(f"共 {n_cookies} 个 Cookie。")
    print("\n验证一下能不能穿墙：")
    print(f"  python main.py doctor --country {cc} --channel meli")
    print("\n注：登录态通常几周后过期，报告里出现「强制登录墙」警告时重跑本工具即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
