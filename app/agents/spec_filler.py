# -*- coding: utf-8 -*-
"""规格补全 Agent —— 给产品补齐芯片/屏幕/内存等规格。

用户要求：
  "这些产品都是已经上市了的，所以你应该是能在网上找到这些产品的规格信息，
   然后去找一些它的竞品的，每个产品至少在每个品牌都有一个对应的竞品"

补两类产品：
  ① 我方产品（用户给的清单只有产品名与代号，没有规格）
  ② 友商产品（抓取中发现的，规格靠标题只能解析出一部分）

★ 最大的风险是模型**编造规格**。
  问"Vega 80 Pro 用什么芯片"，模型不知道也会给一个听起来很像的答案。
  编造的规格进库后，竞品匹配会拿假芯片算档位差 —— 而且完全看不出来。

  三层防护：
   1. 提示词强制要求：不确定的字段**必须返回 null**，宁缺毋滥
   2. 要求同时给出**可验证锚点**（发布年份、首发价位段），
      锚点明显不对的整条降置信度
   3. `spec_source` + `spec_confidence` 一起落库，界面上低置信度明确标出，
      竞品匹配也会把置信度传导到最终评分里
"""
from __future__ import annotations

import json
import logging

from .. import db
from .base import BaseAgent
from .llm import as_dicts, as_float, as_text

log = logging.getLogger("spec_filler")

CATEGORY_FIELDS = {
    "phone": ["chipset", "ram_gb", "rom_gb", "screen_size", "battery_mah",
              "camera_main_mp", "os"],
    "tablet": ["chipset", "ram_gb", "rom_gb", "screen_size", "battery_mah", "os"],
    "pc": ["chipset", "ram_gb", "rom_gb", "screen_size", "os"],
    "wearable": ["screen_size", "battery_mah", "os"],
    "audio": ["battery_mah"],
}


class SpecFillerAgent(BaseAgent):
    name = "spec_filler"
    role = "spec_filler"
    description = "给我方与友商产品补齐规格，标注来源与置信度"

    def run(self, scope: str = "both", limit: int = 200) -> dict:
        self.start(f"规格补全 scope={scope}")
        mine = self._fill_mine(limit) if scope in ("both", "mine") else 0
        rivals = self._fill_rivals(limit) if scope in ("both", "rivals") else 0
        summary = f"补全我方产品 {mine} 个，友商产品 {rivals} 个"
        self.finish("ok", summary, mine + rivals, mine + rivals)
        return {"mine": mine, "rivals": rivals}

    # ------------------------------------------------ 我方产品

    def _fill_mine(self, limit: int, batch: int = 8) -> int:
        rows = db.q("""
            SELECT id, marketing_name, internal_code, category_code, series
            FROM my_product
            WHERE (chipset IS NULL OR chipset='')
              AND status='active'
            ORDER BY category_code, marketing_name LIMIT ?
        """, (limit,))
        if not rows:
            return 0
        if not (self.llm and self.llm.available()):
            self.log_step("我方产品规格补全", parsed={"待补": len(rows)},
                          decision="skipped", status="degraded",
                          reason="未配置 API Key。规格是竞品匹配的必要输入，"
                                 "没有它匹配引擎只能靠价格带，准确度大幅下降")
            return 0

        done = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            listing = "\n".join(
                f"{j}. {r['marketing_name']}"
                f"（{'内部代号 ' + r['internal_code'] if r['internal_code'] else ''}"
                f"{'，' + r['series'] if r['series'] else ''}，品类 {r['category_code']}）"
                for j, r in enumerate(chunk))

            parsed = self.ask_json(
                f"补全我方产品规格 {i}~{i + len(chunk) - 1}",
                self._prompt(listing, "Acme(ACME)"),
                system="你是消费电子产品规格数据库。不确定的字段一律返回 null，"
                       "绝不猜测、绝不编造型号不存在的参数。",
                input_ref=f"my_product:{i}", default=[])

            with db.tx() as conn:
                for item in as_dicts(parsed):
                    try:
                        r = chunk[int(item.get("idx"))]
                    except (TypeError, ValueError, IndexError):
                        continue
                    conf = self._confidence(item)
                    conn.execute("""
                        UPDATE my_product SET chipset=?, screen=?,
                          notes=COALESCE(NULLIF(notes,''),'') || ?,
                          updated_at=datetime('now')
                        WHERE id=?
                    """, (_text(item.get("chipset")),
                          self._screen_text(item),
                          f"[规格由模型补全 置信度{conf:.1f} "
                          f"锚点:{item.get('launch_year') or '?'}]",
                          r["id"]))
                    # RAM/ROM 落到默认 SKU
                    ram, rom = as_float(item.get("ram_gb")), as_float(item.get("rom_gb"))
                    if ram or rom:
                        conn.execute("""
                            INSERT INTO my_sku(product_id,sku_name,ram_gb,rom_gb)
                            VALUES(?,'默认',?,?)
                            ON CONFLICT(product_id, COALESCE(color,''),
                                        COALESCE(ram_gb,-1), COALESCE(rom_gb,-1))
                            DO NOTHING
                        """, (r["id"], int(ram) if ram else None,
                              int(rom) if rom else None))
                    done += 1
        return done

    # ------------------------------------------------ 友商产品

    def _fill_rivals(self, limit: int, batch: int = 8) -> int:
        # ★ 按"规格能不能被用上"排序，而不是按更新时间。
        #   音频类的 spec_weights 是 {'extra': 1.0}，而匹配器会跳过 extra ⇒
        #   total_w=0 ⇒ 规格置信度**恒为 0**，音频产品的规格填了也永远用不上。
        #   旧的 ORDER BY updated_at DESC 在一次全量重算之后毫无区分度，
        #   200 条预算大半花在了音频/PC 上，而手机（用户最关心、规格也最可比）
        #   309 个里只填到 2 个。
        #   现在：手机 > 平板 > PC > 穿戴 > 音频，且优先填**已经进过竞品匹配候选池**的。
        rows = db.q("""
            SELECT rp.id, rp.model_name, rp.category_code, b.name AS brand
            FROM rival_product rp JOIN brand b ON b.id=rp.brand_id
            WHERE (rp.chipset IS NULL OR rp.chipset='')
              AND rp.id IN (SELECT DISTINCT rival_product_id FROM price_obs
                            WHERE rival_product_id IS NOT NULL)
            ORDER BY CASE rp.category_code
                       WHEN 'phone' THEN 0 WHEN 'tablet' THEN 1 WHEN 'pc' THEN 2
                       WHEN 'wearable' THEN 3 ELSE 9 END,
                     rp.updated_at DESC
            LIMIT ?
        """, (limit,))
        if not rows or not (self.llm and self.llm.available()):
            return 0

        done = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            listing = "\n".join(
                f"{j}. {r['brand']} {r['model_name']}（品类 {r['category_code']}）"
                for j, r in enumerate(chunk))

            parsed = self.ask_json(
                f"补全友商产品规格 {i}~{i + len(chunk) - 1}",
                self._prompt(listing, "各友商品牌"),
                system="你是消费电子产品规格数据库。不确定的字段一律返回 null。",
                input_ref=f"rival_product:{i}", default=[])

            with db.tx() as conn:
                for item in as_dicts(parsed):
                    try:
                        r = chunk[int(item.get("idx"))]
                    except (TypeError, ValueError, IndexError):
                        continue
                    conf = self._confidence(item)
                    conn.execute("""
                        UPDATE rival_product SET chipset=?, ram_gb=?, rom_gb=?,
                          screen_size=?, screen_tech=?, battery_mah=?,
                          camera_main_mp=?, os=?, extra_specs=?,
                          spec_source='agent:spec_filler', spec_confidence=?,
                          global_launch_date=COALESCE(global_launch_date,?),
                          updated_at=datetime('now')
                        WHERE id=?
                    """, (_text(item.get("chipset")),
                          _int(item.get("ram_gb")), _int(item.get("rom_gb")),
                          as_float(item.get("screen_size")),
                          _text(item.get("screen_tech")),
                          _int(item.get("battery_mah")),
                          as_float(item.get("camera_main_mp")),
                          _text(item.get("os")),
                          json.dumps({k: v for k, v in item.items()
                                      if k not in ("idx",)}, ensure_ascii=False)[:2000],
                          conf,
                          f"{item.get('launch_year')}-01-01"
                          if item.get("launch_year") else None,
                          r["id"]))
                    done += 1
        return done

    # ------------------------------------------------ 提示词与校验

    @staticmethod
    def _prompt(listing: str, who: str) -> str:
        return (
            f"下面是 {who} 的一批**已上市**消费电子产品。请给出它们的公开规格。\n\n"
            f"{listing}\n\n"
            "★ 硬性要求（违反会导致下游竞品匹配算错，后果严重）：\n"
            "  1. **不确定的字段必须填 null**，绝不猜测、绝不编造。\n"
            "     宁可整条只有一两个字段有值，也不要给看起来合理但其实是编的数字。\n"
            "  2. chipset 填**完整官方名称**（如 \"Snapdragon 8 Gen 3\"、\"Kirin 9020\"、"
            "\"A17 Pro\"、\"Dimensity 9300\"、\"Intel Core Ultra 7 155H\"），不要简写。\n"
            "  3. 同时给出 launch_year（首发年份）作为**可验证锚点** —— "
            "如果你连发布年份都不确定，说明你不熟悉这款产品，其余字段也应该填 null。\n"
            "  4. ram_gb / rom_gb 填该型号**最主流的那个配置**（不是最高配）。\n\n"
            '只返回 JSON 数组，每条：{"idx":序号,"chipset":"芯片全名或null",'
            '"ram_gb":数字或null,"rom_gb":数字或null,"screen_size":英寸数字或null,'
            '"screen_tech":"如 OLED/LCD/AMOLED 或null","battery_mah":数字或null,'
            '"camera_main_mp":数字或null,"os":"如 HarmonyOS 4/Android 14 或null",'
            '"launch_year":年份数字或null,"certainty":"high|medium|low"}')

    @staticmethod
    def _confidence(item: dict) -> float:
        """置信度 = 模型自评 × 字段完整度 × 锚点是否给出"""
        base = {"high": 0.85, "medium": 0.6, "low": 0.35}.get(
            str(item.get("certainty") or "").lower(), 0.5)
        keys = ["chipset", "ram_gb", "rom_gb", "screen_size", "battery_mah"]
        filled = sum(1 for k in keys if item.get(k) not in (None, "", 0))
        completeness = filled / len(keys)
        anchor = 1.0 if item.get("launch_year") else 0.6   # 连年份都没有 → 降权
        return round(base * (0.5 + 0.5 * completeness) * anchor, 2)

    @staticmethod
    def _screen_text(item: dict) -> str | None:
        size = as_float(item.get("screen_size"))
        tech = item.get("screen_tech")
        if not size and not tech:
            return None
        return f"{size}英寸 {tech or ''}".strip()


# 文本规格字段一律过 llm.as_text —— 模型会把 null 写成字符串，
# 落库后 `chipset IS NOT NULL` 判定为真，规格相似度就拿 "null" 去比芯片档位。
# 闸门统一放在 llm.py（那边有完整来龙去脉），这里只留别名。
_text = as_text


def _int(v):
    f = as_float(v)
    return int(f) if f is not None else None
