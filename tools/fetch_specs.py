# -*- coding: utf-8 -*-
"""从 GSMArena 取真实规格，填进我方与友商产品（手机 / 平板 / 穿戴）。

替代"让大模型猜规格"：模型认得出的没有出处，认不出的直接空着。
竞品匹配的规则①（规格相近）建在这些数字上，来源不明是不行的。

★ 站方条款已核对（2026-08-11），本脚本据此设计：
  · **不用搜索端点** /res.php3 —— robots.txt 对所有 UA 禁止。
    改走「品牌列表页 → 机型详情页」，两者均未被禁。
  · **只取我们在跟踪的机型**，不镜像全库（站方 RSL 写着 restrict value extraction）。
  · **限速 + 缓存**：默认 6 秒一次请求，取到的存 spec_source_cache，
    重跑不再打站点。中途停掉可以直接再跑，已完成的不会重来。
  · 每条规格落库时写上来源 URL 与署名（CC BY 4.0）。

用法：
    python tools/fetch_specs.py                  # 看看会做什么（默认试运行）
    python tools/fetch_specs.py --apply          # 真的取
    python tools/fetch_specs.py --apply --delay 8
    python tools/fetch_specs.py --apply --brand samsung --brand xiaomi
    python tools/fetch_specs.py --apply --limit 50
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.scraping.specsource import (BRAND_SLUG, GsmArenaSource,  # noqa: E402
                                     name_variants, normalize_model_key,
                                     trimmed_variants)

SOURCE = "gsmarena"
# GSMArena 覆盖手机 / 平板 / 智能手表（品牌页里混排），PC 与音频不在其内
CATEGORIES = ("phone", "tablet", "wearable")


def load_index(brand: str) -> dict[str, str]:
    rows = db.q("""SELECT model_key, url FROM spec_source_index
                   WHERE source=? AND brand=?""", (SOURCE, brand))
    return {r["model_key"]: r["url"] for r in rows}


def save_index(brand: str, idx: dict[str, str], names: dict[str, str]) -> None:
    with db.tx() as c:
        for k, url in idx.items():
            c.execute("""INSERT INTO spec_source_index(source,brand,model_key,
                             model_name,url)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(source,brand,model_key) DO UPDATE SET
                           url=excluded.url, indexed_at=datetime('now')""",
                      (SOURCE, brand, k, names.get(k, k), url))


def cached_specs(url: str) -> dict | None:
    r = db.q1("SELECT specs_json FROM spec_source_cache WHERE url=?", (url,))
    if not r:
        return None
    try:
        return json.loads(r["specs_json"])
    except Exception:  # noqa: BLE001
        return None


def cache_specs(url: str, specs: dict) -> None:
    with db.tx() as c:
        c.execute("""INSERT INTO spec_source_cache(url,source,model_name,
                         specs_json,attribution)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(url) DO UPDATE SET
                       specs_json=excluded.specs_json,
                       fetched_at=datetime('now')""",
                  (url, SOURCE, specs.get("model_name"),
                   json.dumps(specs, ensure_ascii=False)[:20000],
                   specs.get("attribution")))


def targets(brands: list[str] | None, limit: int | None,
            refresh: bool = False) -> list[dict]:
    """需要补规格的产品：友商 + 我方，限于 GSMArena 覆盖的品类。

    ★ 只挑**已经有价格观测**的友商产品 —— 没有挂牌的产品补了规格也用不上，
      白白多打人家的站。
    """
    qs = ",".join("?" * len(CATEGORIES))
    rows = db.q(f"""
        SELECT rp.id, rp.model_name, rp.category_code, b.name AS brand,
               'rival' AS side, rp.chipset, rp.spec_source
        FROM rival_product rp JOIN brand b ON b.id=rp.brand_id
        WHERE rp.category_code IN ({qs})
          AND rp.id IN (SELECT DISTINCT rival_product_id FROM price_obs
                        WHERE rival_product_id IS NOT NULL)
    """, CATEGORIES)
    mine = db.q(f"""
        SELECT mp.id, mp.marketing_name AS model_name, mp.category_code,
               'Acme' AS brand, 'mine' AS side, mp.chipset, NULL AS spec_source
        FROM my_product mp
        WHERE mp.category_code IN ({qs}) AND mp.status='active'
    """, CATEGORIES)
    # ★ 我方产品在 my_product 里没有 brand 列，上面查询硬写了"Acme"；
    #   而规格源的品牌键是英文 slug —— 不映射的话我方 86 个产品全被判成
    #   "该源无此品牌"，一个都补不到，而这恰恰是竞品匹配里**我方那一侧**。
    for r in mine:
        r["brand"] = "acme"
    out = list(rows) + list(mine)
    if brands:
        want = {b.lower() for b in brands}
        out = [r for r in out if (r["brand"] or "").lower() in want]
    out = [r for r in out if (r["brand"] or "").lower() in BRAND_SLUG
           or r["side"] == "mine"]

    # ★★ 这里原本只有上面那句品牌过滤，注释却写着「已经有 gsmarena 规格的跳过」——
    #   **跳过逻辑根本不存在**。后果：每次运行都按 id 顺序从头处理同一批产品，
    #   已经补好的反复重写，而真正缺日期的那 397 个（spec_source='agent:spec_filler'）
    #   永远排在后面、永远轮不到。实测连跑两轮（80 + 400）写入 324 次，
    #   占位日期一个都没少（530/841 纹丝不动）——**看着在干活，其实在原地打转**。
    if not refresh:
        done = {r["id"] for r in db.q(
            "SELECT id FROM rival_product WHERE spec_source LIKE 'gsmarena%'")}
        out = [r for r in out if not (r["side"] == "rival" and r["id"] in done)]

    # 缺日期的排前面：limit 截断时先花在最需要的产品上
    out.sort(key=lambda r: 0 if r.get("spec_source") == "agent:spec_filler" else 1)
    if limit:
        out = out[:limit]
    return out


def write_specs(row: dict, specs: dict) -> None:
    """把规格写回产品表，并记来源。

    ★ 覆盖策略：GSMArena 的数据**优先于**模型猜的（spec_source='agent:spec_filler'），
      但不覆盖人工填的（'manual'）—— 人工是最高权威。
    """
    if row["side"] == "rival":
        cur = db.q1("SELECT spec_source FROM rival_product WHERE id=?", (row["id"],))
        if cur and (cur["spec_source"] or "").startswith("manual"):
            return
        with db.tx() as c:
            c.execute("""
                UPDATE rival_product SET
                  chipset=COALESCE(?,chipset), ram_gb=COALESCE(?,ram_gb),
                  rom_gb=COALESCE(?,rom_gb), screen_size=COALESCE(?,screen_size),
                  screen_tech=COALESCE(?,screen_tech),
                  battery_mah=COALESCE(?,battery_mah),
                  camera_main_mp=COALESCE(?,camera_main_mp),
                  os=COALESCE(?,os),
                  -- ★ 首发日期这里**覆盖**而不是 COALESCE。
                  --   库里原有的值来自情报 Agent，取的是**新闻发布日期**——
                  --   一条 2020 年的旧闻会让 2025 年的机器首发日期记成 2020，
                  --   上市看板的"全球首发→拉美滞后天数"整个算错。
                  --   GSMArena 给的是厂商公布的发布日，有出处，应当压过猜的。
                  global_launch_date=COALESCE(?,global_launch_date),
                  extra_specs=?, spec_source=?, spec_confidence=0.95,
                  updated_at=datetime('now')
                WHERE id=?""",
                      (specs.get("chipset"), specs.get("ram_gb"), specs.get("rom_gb"),
                       specs.get("screen_size"), specs.get("screen_tech"),
                       specs.get("battery_mah"), specs.get("camera_main_mp"),
                       specs.get("os"), specs.get("launch_date"),
                       # ★ 把**全部** data-spec 字段都存下来（46+ 项：网络制式、
                       #   机身尺寸重量、传感器、快充、材质、防水、音频、定位…）。
                       #   只留映射过的那 8 个字段，等于每次想多比一个维度就得重抓一遍；
                       #   raw 存着，以后加维度直接从库里取。
                       json.dumps({"memory_options": specs.get("memory_options"),
                                   "source_url": specs.get("source_url"),
                                   "attribution": specs.get("attribution"),
                                   "all": specs.get("raw") or {}},
                                  ensure_ascii=False)[:12000],
                       f"{SOURCE}:{specs.get('source_url', '')}"[:200],
                       row["id"]))
    else:
        with db.tx() as c:
            c.execute("""UPDATE my_product SET chipset=COALESCE(?,chipset),
                             updated_at=datetime('now') WHERE id=?""",
                      (specs.get("chipset"), row["id"]))
            ram, rom = specs.get("ram_gb"), specs.get("rom_gb")
            if ram or rom:
                c.execute("""INSERT INTO my_sku(product_id,sku_name,ram_gb,rom_gb)
                             VALUES(?,'默认',?,?)
                             ON CONFLICT(product_id, COALESCE(color,''),
                                         COALESCE(ram_gb,-1), COALESCE(rom_gb,-1))
                             DO NOTHING""", (row["id"], ram, rom))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的执行（默认只试运行）")
    ap.add_argument("--delay", type=float, default=6.0, help="请求间隔秒（默认 6）")
    ap.add_argument("--brand", action="append", help="只做这些品牌，可重复")
    ap.add_argument("--limit", type=int, help="最多处理多少个产品")
    ap.add_argument("--reindex", action="store_true", help="强制重建品牌索引")
    ap.add_argument("--refresh", action="store_true",
                    help="连已有 gsmarena 规格的也重取（默认跳过）")
    ap.add_argument("--engine", choices=("http", "selenium"), default="http",
                    help="取页方式。默认 http（实测 289 页 0 失败 0 限流，"
                         "该站无挑战、规格在服务端 HTML 里）；"
                         "selenium 慢 5~10 倍，留给站方将来加防护时用")
    args = ap.parse_args()

    rows = targets(args.brand, args.limit, getattr(args, 'refresh', False))
    by_brand: dict[str, list[dict]] = {}
    for r in rows:
        by_brand.setdefault((r["brand"] or "").lower(), []).append(r)

    print(f"待补规格的产品 {len(rows)} 个（限 {', '.join(CATEGORIES)}，"
          f"且必须已有价格观测）")
    for b, items in sorted(by_brand.items(), key=lambda x: -len(x[1])):
        known = "✔" if b in BRAND_SLUG else "✗ 该源无此品牌"
        print(f"  {b:12} {len(items):4}  {known}")

    if not args.apply:
        print("\n[试运行] 没有发出任何请求。加 --apply 才真的取。")
        print(f"  预计请求数 ≈ 品牌索引页 + {len(rows)} 个机型页")
        print(f"  按 {args.delay:.0f} 秒/次算，约 "
              f"{(len(rows) + 40) * args.delay / 60:.0f} 分钟")
        print("  站方条款：不使用被 robots 禁止的搜索端点；只取我们跟踪的机型；"
              "结果标注 GSMArena.com (CC BY 4.0)")
        return 0

    eng = None
    if args.engine == "selenium":
        from app.config import load_runtime
        from app.scraping.engine import ScrapeEngine
        eng = ScrapeEngine(load_runtime()["scrape"])
        eng.__enter__()
        print("使用浏览器引擎取页（比 HTTP 慢 5~10 倍）")
    try:
        _run(args, by_brand, eng)
    finally:
        if eng is not None:
            eng.__exit__(None, None, None)
    return 0


def _run(args, by_brand, eng) -> None:
    # ★★ 这两个变量原本从未定义：一旦走到「命中缓存 / 索引里找不到」分支就
    #   NameError 当场崩。也就是说**这个工具从来没跑完过一次** ——
    #   而它正是"有出处的首发日期"的唯一来源。
    #   后果：库里 529 个产品一直留着 agent:spec_filler 补的 1月1日 占位日期
    #   （该来源 529/529 = 100% 是占位），上市看板的前瞻清单只能靠
    #   `spec_source LIKE 'gsmarena%'` 硬筛，能用的只剩 60 个。
    #   崩溃发生在第一个品牌之后，日志尾巴看着像"跑完了"，很难察觉。
    stat: Counter = Counter()
    misses: list[str] = []

    with GsmArenaSource(delay=args.delay, engine=eng) as g:
        for brand, items in sorted(by_brand.items(), key=lambda x: -len(x[1])):
            slug = BRAND_SLUG.get(brand)
            if not slug:
                stat["品牌不在该源"] += len(items)
                continue

            idx = {} if args.reindex else load_index(brand)
            if not idx:
                print(f"[{brand}] 建索引…")
                idx = g.brand_index(brand)
                save_index(brand, idx, {})
            print(f"[{brand}] 索引 {len(idx)} 个机型，待补 {len(items)} 个产品")

            for r in items:
                # ★ 两边的"短名"约定基本一致（我们存不带品牌的、站点列表页也是短名），
                #   但历史数据里有少量还带着品牌前缀（"Lenovo Tab One" vs 索引 "tabone"），
                #   所以带品牌、去品牌两种写法都试一遍。
                name = r["model_name"] or ""
                bases = list(name_variants(name))
                low, bl = name.lower(), (r["brand"] or "").lower()
                if bl and low.startswith(bl + " "):
                    bases.extend(name_variants(name[len(bl):].strip()))
                bases.append(f"{r['brand']} {name}")
                # ★ 每个写法再逐段砍尾巴，**长的先试**。
                #   型号名尾巴上常粘着规格/营销词（"A5 Pro IP69"、"A60 Ai Camera"），
                #   而上层只接受精确命中 —— 所以砍是安全的：
                #   "A5 Pro IP69" 会停在 "A5 Pro"，不会掉到 "A5"（那是另一款机器）。
                cands = []
                for b in bases:
                    for v in trimmed_variants(b):
                        if v not in cands:
                            cands.append(v)
                url = next((idx[k] for k in map(normalize_model_key, cands)
                            if k in idx), None)
                if not url:
                    stat["索引里找不到"] += 1
                    misses.append(f"{r['brand']} {name}")
                    continue

                specs = cached_specs(url)
                if specs:
                    stat["命中缓存"] += 1
                else:
                    specs = g.device_specs(url)
                    if not specs:
                        stat["取页失败"] += 1
                        continue
                    cache_specs(url, specs)
                    stat["新取"] += 1

                write_specs(r, specs)
                stat["已写入"] += 1
                if stat["已写入"] % 25 == 0:
                    print(f"    …已写入 {stat['已写入']} 个  {dict(g.stats)}")

    print("\n完成：")
    for k, v in stat.most_common():
        print(f"  {k:14} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
