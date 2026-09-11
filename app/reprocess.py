# -*- coding: utf-8 -*-
"""数据回填：用当前的归一化规则重新处理已入库的数据。

★ 为什么必须有这个工具：
  修好解析逻辑 ≠ 修好数据。归一化规则改对了，但已入库的记录还是按旧规则算的。
  实例：归一化第一版把颜色当型号，库里留下了
      Z Flip8 Crema / Z Flip8 Rosa / Z Flip8   ← 同一台机器三条记录
  规则修好后新数据是对的，但这三条旧记录不会自己合并，
  价格依然是散的、中位数依然算不出来 —— 而且随着新数据进来越来越难分辨哪些是脏的。

做三件事：
  1. 用当前规则重算所有 rival_product 的 model_name / model_key
  2. 合并 model_key 相同的记录（价格观测迁移到主记录，重复记录删除）
  3. 重新挂接 price_obs 到正确的 rival_product

幂等：重跑不会产生副作用。
"""
from __future__ import annotations

import json
import logging
import re

from . import db
from .agents.cleaner import CleanerAgent

log = logging.getLogger("reprocess")


def renormalize_all(dry_run: bool = False) -> dict:
    """重新归一化所有友商产品并合并重复项。"""
    brands = {b["id"]: b for b in db.q("SELECT * FROM brand")}
    products = db.q("""SELECT rp.*, b.name AS brand_name, b.aliases
                       FROM rival_product rp JOIN brand b ON b.id = rp.brand_id
                       ORDER BY rp.id""")
    if not products:
        return {"products": 0}

    report = {"products": len(products), "renamed": 0, "merged": 0,
              "obs_moved": 0, "deleted": 0, "changes": [], "dry_run": dry_run}

    # 第一步：算出每条记录的新型号名
    # 用 price_obs 里的原始标题重算（rival_product.model_name 已经是旧规则的产物，
    # 拿它再归一化一次会越洗越糟）
    new_key_map: dict[int, tuple[str, str]] = {}
    for p in products:
        try:
            aliases = json.loads(p["aliases"] or "[]")
        except Exception:  # noqa: BLE001
            aliases = []

        sample = db.q1("""SELECT title FROM price_obs
                          WHERE rival_product_id=? AND title IS NOT NULL
                          ORDER BY length(title) DESC LIMIT 1""", (p["id"],))
        source_title = (sample or {}).get("title") or p["model_name"]
        new_name = CleanerAgent.normalize_model(source_title, aliases, p["brand_name"])
        if not new_name or len(new_name) < 2:
            new_name = p["model_name"]
        new_key = re.sub(r"[^a-z0-9]", "", new_name.lower())
        new_key_map[p["id"]] = (new_name, new_key)
        if new_key != p["model_key"]:
            report["changes"].append({
                "id": p["id"], "brand": p["brand_name"],
                "old": p["model_name"], "new": new_name,
                "source_title": source_title[:70],
            })

    # 第二步：按 (brand, new_key, category) 分组，组内选一个主记录
    groups: dict[tuple, list[dict]] = {}
    for p in products:
        _, new_key = new_key_map[p["id"]]
        groups.setdefault((p["brand_id"], new_key, p["category_code"]), []).append(p)

    if dry_run:
        report["merged"] = sum(len(v) - 1 for v in groups.values() if len(v) > 1)
        report["renamed"] = len(report["changes"])
        return report

    for (brand_id, new_key, cat), members in groups.items():
        # 主记录：规格最全的那条（合并时保留信息最多的）
        members.sort(key=lambda m: (
            -sum(1 for f in ("chipset", "ram_gb", "rom_gb", "screen_size",
                             "battery_mah", "global_launch_date") if m.get(f)),
            m["id"]))
        primary = members[0]
        new_name, _ = new_key_map[primary["id"]]

        with db.tx() as conn:
            # ★ 顺序不能反：必须【先清掉重复记录，再给主记录改名】。
            #   若某条重复记录当前正好占着 new_key，先改名会撞
            #   UNIQUE(brand_id, model_key, category_code)。
            #   这是所有「合并 + 重命名」操作的通用陷阱。
            for dup in members[1:]:
                # 把重复记录的数据迁到主记录
                for table in ("price_obs", "review", "review_profile",
                              "launch_event", "voc_insight", "competitor_match",
                              "product_page_cache", "rival_sku"):
                    try:
                        cur = conn.execute(
                            f"UPDATE OR IGNORE {table} SET rival_product_id=? "
                            f"WHERE rival_product_id=?", (primary["id"], dup["id"]))
                        if table == "price_obs":
                            report["obs_moved"] += cur.rowcount or 0
                    except Exception as e:  # noqa: BLE001
                        log.debug("迁移 %s 失败: %s", table, str(e)[:80])
                # 主记录缺的规格用重复记录补上
                for f in ("chipset", "ram_gb", "rom_gb", "screen_size",
                          "battery_mah", "camera_main_mp", "os", "global_launch_date"):
                    if not primary.get(f) and dup.get(f):
                        conn.execute(
                            f"UPDATE rival_product SET {f}=? WHERE id=? AND "
                            f"({f} IS NULL OR {f}='')", (dup[f], primary["id"]))
                conn.execute("DELETE FROM rival_product WHERE id=?", (dup["id"],))
                report["merged"] += 1
                report["deleted"] += 1

            # 重复记录清完了，现在改名不会撞唯一约束
            if primary["model_key"] != new_key or primary["model_name"] != new_name:
                try:
                    conn.execute("""UPDATE rival_product SET model_name=?, model_key=?,
                                    updated_at=datetime('now') WHERE id=?""",
                                 (new_name, new_key, primary["id"]))
                    report["renamed"] += 1
                except Exception as e:  # noqa: BLE001
                    # 仍冲突说明另一个【分组】已占用该键（跨组撞名）。
                    # 把本组并进去，而不是让整次回填失败。
                    log.warning("重命名 %s → %s 冲突，尝试并入已有记录: %s",
                                primary["model_name"], new_name, str(e)[:90])
                    other = conn.execute(
                        "SELECT id FROM rival_product WHERE brand_id=? AND model_key=?"
                        " AND category_code=?",
                        (brand_id, new_key, cat)).fetchone()
                    if other and other[0] != primary["id"]:
                        for table in ("price_obs", "review", "review_profile",
                                      "launch_event", "voc_insight", "competitor_match",
                                      "product_page_cache", "rival_sku"):
                            try:
                                conn.execute(
                                    f"UPDATE OR IGNORE {table} SET rival_product_id=? "
                                    f"WHERE rival_product_id=?", (other[0], primary["id"]))
                            except Exception:  # noqa: BLE001
                                pass
                        conn.execute("DELETE FROM rival_product WHERE id=?",
                                     (primary["id"],))
                        report["merged"] += 1

    # 第三步：把 price_obs 的 model_guess 同步成新型号名
    with db.tx() as conn:
        conn.execute("""
            UPDATE price_obs SET model_guess = (
                SELECT model_name FROM rival_product WHERE id = price_obs.rival_product_id)
            WHERE rival_product_id IS NOT NULL
        """)
    return report


def relink_orphans() -> dict:
    """把还没挂上友商产品的价格观测重新挂接（品牌识别后再归一化）。"""
    from .agents.cleaner import CleanerAgent as _C
    agent = _C()
    dates = db.q("""SELECT DISTINCT obs_date FROM price_obs
                    WHERE rival_product_id IS NULL AND brand_id IS NOT NULL
                    ORDER BY obs_date DESC LIMIT 60""")
    linked = created = 0
    for d in dates:
        a, b = agent._normalize_and_link(d["obs_date"])
        linked += a
        created += b
    return {"linked": linked, "created": created}


def full_reprocess(dry_run: bool = False) -> dict:
    r1 = renormalize_all(dry_run)
    if dry_run:
        return {"renormalize": r1}
    r2 = relink_orphans()
    r3 = renormalize_all(False)   # 新挂接的可能又产生重复，再合一次
    return {"renormalize": r1, "relink": r2, "second_pass": {
        "merged": r3["merged"], "renamed": r3["renamed"]}}
