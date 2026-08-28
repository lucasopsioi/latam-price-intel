# -*- coding: utf-8 -*-
"""拉美竞品情报中枢 —— 命令行入口

  python main.py init            建库 + 灌配置（幂等，改完 YAML 重跑即可）
  python main.py status          看当前库里有什么
  python main.py channels        列出所有渠道
  python main.py selftest        跑离线自测（不联网）
  python main.py serve           启动本地界面（默认 http://127.0.0.1:8765）
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config, db  # noqa: E402


def setup_logging(verbose: bool = False) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(
            config.LOG_DIR / f"run-{db.today().replace('-', '')}.log", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S", handlers=handlers, force=True)


def cmd_init(_args) -> int:
    print("正在建库…")
    db.init_db()
    print(f"数据库: {config.DB_PATH}")
    cmd_status(None)
    return 0


def cmd_status(_args) -> int:
    rows = db.q("SELECT code,name_zh,currency FROM country ORDER BY sort_order")
    print(f"\n国家 ({len(rows)}):")
    for r in rows:
        n = db.q1("SELECT COUNT(*) c FROM channel WHERE country_code=? AND enabled=1",
                  (r["code"],))["c"]
        print(f"  {r['code']}  {r['name_zh']:<6} {r['currency']}   渠道 {n} 个")

    cats = db.q("SELECT code,name_zh,icon FROM category ORDER BY sort_order")
    print(f"\n产业 ({len(cats)}): " + "  ".join(f"{c['icon']}{c['name_zh']}" for c in cats))

    brands = db.q("SELECT name,is_ours FROM brand ORDER BY is_ours DESC, name")
    ours = [b["name"] for b in brands if b["is_ours"]]
    rivals = [b["name"] for b in brands if not b["is_ours"]]
    print(f"\n我方品牌: {', '.join(ours) or '（无）'}")
    print(f"友商品牌 ({len(rivals)}): {', '.join(rivals)}")

    tables = {
        "我方产品": "my_product", "友商产品": "rival_product",
        "价格观测": "price_obs", "竞品匹配": "competitor_match",
        "上市事件": "launch_event", "情报动态": "dynamics",
        "抓取批次": "scrape_run", "Agent运行": "agent_run",
    }
    print("\n数据量:")
    for label, t in tables.items():
        try:
            c = db.q1(f"SELECT COUNT(*) c FROM {t}")["c"]
        except Exception:  # noqa: BLE001
            c = "—"
        print(f"  {label:<10} {c}")
    return 0


def cmd_channels(_args) -> int:
    rows = db.q("""
        SELECT c.country_code, c.code, c.name, c.kind, c.adapter,
               c.default_seller_type, c.priority, c.enabled, c.search_url
        FROM channel c JOIN country co ON co.code=c.country_code
        ORDER BY co.sort_order, c.priority
    """)
    cur = None
    for r in rows:
        if r["country_code"] != cur:
            cur = r["country_code"]
            print(f"\n=== {cur} ===")
        flag = " " if r["enabled"] else "×"
        print(f" {flag} {r['name']:<26} {r['kind']:<12} adapter={r['adapter']:<10} "
              f"卖家默认={r['default_seller_type']}")
    print(f"\n共 {len(rows)} 个渠道（× = 已关闭）")
    return 0


def cmd_doctor(args) -> int:
    """渠道体检：实测每个渠道的搜索 URL 还能不能用。会真实联网。"""
    from app.scraping.engine import ScrapeEngine
    from app.scraping.health import check_channel, print_report, save_health

    db.init_db()
    where, params = "WHERE c.enabled=1", []
    if args.country:
        where += " AND c.country_code=?"
        params.append(args.country.upper())
    if args.channel:
        where += " AND c.code=?"
        params.append(args.channel.lower())

    channels = db.q(f"""
        SELECT c.* FROM channel c JOIN country co ON co.code=c.country_code
        {where} ORDER BY co.sort_order, c.priority
    """, params)
    if args.limit:
        channels = channels[: args.limit]
    if not channels:
        print("没有匹配的渠道")
        return 1

    countries = {c["code"]: c for c in db.q("SELECT * FROM country")}
    cfg = config.load_runtime()["scrape"]
    if args.show_browser:
        cfg = {**cfg, "headless": False}
    meli_token = db.get_setting("meli_access_token", "")

    print(f"开始体检 {len(channels)} 个渠道（真实联网，每站 5~20 秒）…")
    if not meli_token:
        print("提示：未配置 MercadoLibre token，ML 只能走网页通道（大概率撞登录墙）")

    results = []
    with ScrapeEngine(cfg) as engine:
        for i, ch in enumerate(channels, 1):
            co = countries[ch["country_code"]]
            print(f"  [{i}/{len(channels)}] {co['code']} {ch['name']} …", flush=True)
            r = check_channel(engine, ch, co, meli_token)
            results.append(r)
            print(f"        → {r['status']}  {r['items']} 条  {r['ms']/1000:.1f}s")
        print("\n引擎统计:", engine.summary())

    save_health(results)
    print_report(results)
    return 0


def cmd_reprocess(args) -> int:
    """用当前的归一化规则重新处理已入库数据（修好规则后回填历史脏数据）"""
    from app import reprocess
    db.init_db()

    print("先做一次试算（不写库）…\n")
    dry = reprocess.renormalize_all(dry_run=True)
    print(f"友商产品共 {dry['products']} 个")
    print(f"型号名会变更：{dry['renamed']} 个")
    print(f"会合并掉的重复记录：{dry['merged']} 个\n")

    if dry["changes"]:
        print("变更样例（最多 25 条）：")
        for c in dry["changes"][:25]:
            print(f"  {c['brand']:<10} 「{c['old']}」→「{c['new']}」")
            print(f"             源标题: {c['source_title']}")
        print()

    if args.dry_run:
        print("（--dry-run 模式，未写库）")
        return 0
    if not dry["renamed"] and not dry["merged"]:
        print("没有需要回填的数据。")
        return 0

    print("开始回填…")
    r = reprocess.full_reprocess(dry_run=False)
    print(f"  重命名 {r['renormalize']['renamed']} 个")
    print(f"  合并重复 {r['renormalize']['merged']} 个（迁移价格观测 "
          f"{r['renormalize']['obs_moved']} 条）")
    print(f"  重新挂接孤立观测：{r['relink']['linked']} 条，新建产品 "
          f"{r['relink']['created']} 个")
    print(f"  二次合并：{r['second_pass']['merged']} 个")

    n = db.q1("SELECT COUNT(*) c FROM rival_product")["c"]
    print(f"\n回填后友商产品数：{n}")
    for row in db.q("""SELECT b.name brand, rp.model_name,
                              (SELECT COUNT(*) FROM price_obs
                                WHERE rival_product_id=rp.id) n
                       FROM rival_product rp JOIN brand b ON b.id=rp.brand_id
                       ORDER BY n DESC LIMIT 20"""):
        print(f"  {row['brand']:<11}{row['model_name']:<32}{row['n']} 条价格")
    return 0


def cmd_selftest(_args) -> int:
    """离线自测：不联网，验证解析层与数据库层是否健康。"""
    import subprocess
    root = Path(__file__).resolve().parent
    ok = True

    print("=== 1/3 解析层回归测试 ===")
    r = subprocess.run([sys.executable, str(root / "tests" / "test_extract.py")],
                       capture_output=True, text=True, encoding="utf-8")
    print(r.stdout.strip()[-800:])
    ok = ok and r.returncode == 0

    print("\n=== 2/3 数据库读写 ===")
    try:
        db.init_db()
        db.set_setting("_selftest", "hello", is_secret=False)
        assert db.get_setting("_selftest") == "hello"
        db.set_setting("_selftest_secret", "sk-ABCDEFG1234567", is_secret=True)
        assert db.get_setting("_selftest_secret") == "sk-ABCDEFG1234567"
        masked = [s for s in db.list_settings_masked() if s["key"] == "_selftest_secret"][0]
        assert "ABCDEFG" not in masked["value"], "掩码泄露了密钥内容"
        print(f"  建库/读写/DPAPI加密/掩码  OK  (掩码样例: {masked['value']})")
    except Exception as e:  # noqa: BLE001
        print(f"  失败: {e}")
        ok = False

    print("\n=== 3/3 抓取引擎可用性（不发请求）===")
    try:
        from app.scraping.engine import ScrapeEngine
        cfg = config.load_runtime()["scrape"]
        eng = ScrapeEngine(cfg)
        print(f"  引擎装配 OK  主={eng._primary.name} 兜底={'selenium' if eng._fallback_enabled else '关闭'}")
        try:
            import playwright  # noqa: F401
            print("  playwright 已安装")
        except ImportError:
            print("  ! playwright 未安装")
            ok = False
        try:
            import undetected_chromedriver  # noqa: F401
            print("  undetected-chromedriver 已安装")
        except ImportError:
            print("  ! undetected-chromedriver 未安装（兜底会退回原生 selenium）")
    except Exception as e:  # noqa: BLE001
        print(f"  失败: {e}")
        ok = False

    print("\n" + ("自测通过" if ok else "自测有失败项，见上"))
    return 0 if ok else 1


def cmd_serve(args) -> int:
    import uvicorn
    from app import config, livelog, scheduler, tgbot
    from app.agents import LLMClient
    from app.api.server import app as fastapi_app
    db.init_db()
    # ★★ 先判断"是不是真有别的进程正在采集"，再决定收不收僵尸。
    #   原来的前提是"服务重启 = 上个进程已经没了"，但这个前提有个真实的例外：
    #   用户/开发者另开一个进程手工跑采集，同时重启服务。
    #   实测事故：手工跑Acme商城采集时重启服务，
    #   ① 那一轮被标成 interrupted（数据照常入库，但状态是错的）；
    #   ② 更糟的是下面的浏览器回收器把**正在用的** Chrome 当孤儿杀了 ——
    #      每个国家第一个单元成功、之后全部 failed，看起来像站点反爬。
    #   判据用"最近有没有采集单元落库"：活着的采集每隔几十秒就会写一条，
    #   3 分钟内有写入 = 有进程正在跑，此时两件事都不该做。
    busy = db.q1("""SELECT COUNT(*) n FROM scrape_unit
                    WHERE created_at >= datetime('now','-3 minute')""") or {}
    collecting = bool(busy.get("n"))
    if collecting:
        print(f"检测到最近 3 分钟有 {busy['n']} 个采集单元落库 —— "
              f"判定有进程正在采集，跳过僵尸轮次回收与浏览器回收")
    else:
        # 服务重启说明上个进程已经没了 —— 把它留下的 running 轮次收掉，
        # 否则运行记录页会永远挂着一堆"运行中"（实测 19 轮里 18 轮是僵尸）
        n = db.reconcile_dangling_runs()
        if n:
            print(f"回收上个进程遗留的采集轮次 {n} 个")
    # ★ 数据行要收，进程也要收：服务被杀时 Selenium 起的 Chrome 不会跟着退，
    #   继续占着 user-data-dir，下次启动一路报 "chrome not reachable" 空转。
    #   实测因此把一整轮采集耗成 0 条。
    try:
        if not collecting:          # 上面判过：有进程在采集时一个都不能杀
            from app.scraping.selenium_driver import reap_orphan_browsers
            k = reap_orphan_browsers()
            if k:
                print(f"回收上个进程遗留的抓取浏览器 {k} 个")
    except Exception as e:  # noqa: BLE001
        print(f"（回收残留浏览器失败，不影响启动：{type(e).__name__}）")
    livelog.install()          # 抓取过程实时推到界面
    sched = scheduler.start()
    # Telegram 双向：之前只有推送（sendMessage），发消息给它没反应。
    # 这里起一个长轮询线程收 /status /price 之类的指令。
    tg_on = tgbot.start(LLMClient(config.load_runtime()["agents"]))
    print(f"界面地址: http://127.0.0.1:{args.port}")
    if sched:
        for j in sched.get_jobs():
            print(f"  定时任务 {j.id}：下次 {j.next_run_time}")
    else:
        print("  定时任务未启用（设置页可开）")
    print("  Telegram 收信：" + ("已启动，可发 /help" if tg_on else "未配置"))
    try:
        uvicorn.run(fastapi_app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        scheduler.stop()
        tgbot.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="main.py", description="拉美竞品情报中枢")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("init", help="建库 + 灌配置")
    sub.add_parser("status", help="看库里有什么")
    sub.add_parser("channels", help="列出所有渠道")
    sub.add_parser("selftest", help="离线自测")
    rp = sub.add_parser("reprocess", help="用当前归一化规则回填历史数据")
    rp.add_argument("--dry-run", action="store_true", help="只试算不写库")
    dp = sub.add_parser("doctor", help="渠道体检（真实联网）")
    dp.add_argument("--country", help="只测某国，如 MX")
    dp.add_argument("--channel", help="只测某渠道，如 liverpool")
    dp.add_argument("--limit", type=int, help="最多测几个")
    dp.add_argument("--show-browser", action="store_true", help="显示浏览器窗口")
    sp = sub.add_parser("serve", help="启动本地界面")
    sp.add_argument("--port", type=int, default=8765)

    args = p.parse_args()
    setup_logging(args.verbose)
    if not args.cmd:
        p.print_help()
        return 0

    db.init_db() if args.cmd in ("status", "channels") else None
    return {
        "init": cmd_init, "status": cmd_status, "channels": cmd_channels,
        "selftest": cmd_selftest, "doctor": cmd_doctor, "serve": cmd_serve,
        "reprocess": cmd_reprocess,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
