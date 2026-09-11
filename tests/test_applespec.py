# -*- coding: utf-8 -*-
"""苹果规格源（hubweb.cn）的回归测试。

用户 2026-09-03 指定该源：「苹果的 RAM 我有个网站，Hubweb.cn，
这个网站上有所有苹果产品的参数」。

为什么苹果要单独一个源：苹果**从不在商品标题里标 RAM**
（"iPhone 17 Pro Max 256GB" 不写 12GB），拉美渠道列表页也没有，
标题解析永远拿不到；而没有 RAM 就没法做同配置比价。

★ 让苹果可安全填充的关键性质：**同一 iPhone 机型的 RAM 不随存储变化**
  （17 Pro 无论 256G/512G/1TB 都是 12GB）。安卓不成立（Galaxy A17
  128G→4GB、256G→8GB），所以那边只能同变体推断。
  ⚠ 但 **iPad Pro 是例外**：256/512G→12G，1T/2T→16G，必须按容量取档。
"""
import io
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


from app.scraping.applespec import (clean_model, match_ipad,  # noqa: E402
                                    match_model, ram_for)

# ─────────── 1. 机型名清洗 ───────────
ok(clean_model("iPhone 17 Pro Max 宣传图 / 内部图 1 / 内部图 2")
   == "iPhone 17 Pro Max",
   "★ li 首行会粘上页面附件文案（宣传图/内部图），必须剥掉")
ok(clean_model("iPad Pro 13 英寸 (M5)") == "iPad Pro 13 英寸 (M5)",
   "正常机型名不能被剥坏")

# ─────────── 2. 按容量取 RAM 档 ───────────
iphone = {"variants": [{"ram_gb": 12, "caps": [256, 512, 1024, 2048]}]}
ipadpro = {"variants": [{"ram_gb": 12, "caps": [256, 512]},
                        {"ram_gb": 16, "caps": [1024, 2048]}]}
ok(ram_for(iphone, None) == 12,
   "★ 单档机型（iPhone）不需要知道容量就能给 RAM —— 这正是苹果可安全填充的原因")
ok(ram_for(iphone, 512) == 12, "给了容量也应一致")
ok(ram_for(ipadpro, 512) == 12, "iPad Pro 512G → 12G")
ok(ram_for(ipadpro, 1024) == 16, "iPad Pro 1T → 16G")
ok(ram_for(ipadpro, None) is None,
   "★ 多档机型不知道容量时必须返回 None —— 猜一个就是编造规格")
ok(ram_for(ipadpro, 128) is None,
   "★ 容量对不上任何一档也不猜（128G 不是 iPad Pro 的档位）")
ok(ram_for({"variants": []}, 256) is None, "无档位数据不炸也不猜")

# ─────────── 3. 型号匹配：宁缺勿错 ───────────
specs = [
    {"model": "iPhone 17", "key": "iphone17", "category": "phone",
     "variants": [{"ram_gb": 8, "caps": [256, 512]}]},
    {"model": "iPhone 17 Pro", "key": "iphone17pro", "category": "phone",
     "variants": [{"ram_gb": 12, "caps": [256, 512, 1024]}]},
    {"model": "iPhone 17 Pro Max", "key": "iphone17promax", "category": "phone",
     "variants": [{"ram_gb": 12, "caps": [256, 512, 1024, 2048]}]},
]
ok((match_model("iPhone 17 Pro Max", specs) or {}).get("model") == "iPhone 17 Pro Max",
   "精确匹配")
ok((match_model("Apple iPhone 17 Pro", specs) or {}).get("model") == "iPhone 17 Pro",
   "库里带 Apple 前缀也要能匹配")
m17 = match_model("iPhone 17", specs)
ok(m17 is not None and m17["model"] == "iPhone 17",
   "★ 'iPhone 17' 必须精确命中 17 而不是 17 Pro —— RAM 差 8 vs 12 一整档")

# ─────────── 4. iPad 要素匹配 ───────────
ipads = [
    {"model": "iPad Air 11 英寸 (M4)", "category": "tablet",
     "variants": [{"ram_gb": 12, "caps": [128, 256, 512, 1024]}]},
    {"model": "iPad Air 13 英寸 (M4)", "category": "tablet",
     "variants": [{"ram_gb": 12, "caps": [128, 256, 512, 1024]}]},
    {"model": "iPad Air 11 英寸 (M3)", "category": "tablet",
     "variants": [{"ram_gb": 8, "caps": [256, 512, 1024]}]},
    {"model": "iPad (A16)", "category": "tablet",
     "variants": [{"ram_gb": 6, "caps": [256, 512]}]},
    {"model": "iPad (第 9 代)", "category": "tablet",
     "variants": [{"ram_gb": 3, "caps": [64, 256]}]},
    {"model": "iPad mini (A17 Pro)", "category": "tablet",
     "variants": [{"ram_gb": 8, "caps": [256, 512]}]},
]
for db_name, want in [
    ("Apple iPad Air 11 M4", "iPad Air 11 英寸 (M4)"),
    ("Apple iPad Air 13 M4", "iPad Air 13 英寸 (M4)"),
    ("Apple iPad Air 11 M3", "iPad Air 11 英寸 (M3)"),
    ("Apple iPad A16 (11th Gen)", "iPad (A16)"),
    ("Apple iPad 9th Gen", "iPad (第 9 代)"),
    ("Apple iPad Mini A17 Pro", "iPad mini (A17 Pro)"),
]:
    got = match_ipad(db_name, ipads)
    ok(got is not None and got["model"] == want,
       f"iPad 匹配：{db_name} 应得 {want}，实得 {(got or {}).get('model')}")

# ★ 型号名不含代次/芯片的，绝不能瞎配
for vague in ("Apple iPad", "Apple iPad Air", "iPad Mini"):
    ok(match_ipad(vague, ipads) is None,
       f"★ '{vague}' 信息不足以定代次，必须返回 None（填了就是猜）")

# ★ 「11th Gen」不能被当成 11 英寸
from app.scraping.applespec import _facets  # noqa: E402

f = _facets("Apple iPad 9th Gen")
ok(f["gen"] == 9 and f["size"] is None,
   f"★ 代次数字不能被当尺寸解析，实得 {f}")
f2 = _facets("Apple iPad Air 11 M4")
ok(f2["series"] == "air" and f2["size"] == 11.0 and f2["chip"] == "m4",
   f"★ Apple 前缀要剥掉才认得出系列，实得 {f2}")

# ─────────── 5. 规格表落地与接线 ───────────
sf = ROOT / "data/apple_specs.json"
ok(sf.exists(), "苹果规格表要落到 data/apple_specs.json")
if sf.exists():
    data = json.loads(io.open(sf, encoding="utf-8").read())
    ok(len(data) >= 60, f"至少 60 款机型，实得 {len(data)}")
    ok(all(x.get("variants") for x in data), "每款都要有 RAM 档位")
    p17 = next((x for x in data if x["model"] == "iPhone 17 Pro"), None)
    ok(p17 and p17["variants"][0]["ram_gb"] == 12,
       "iPhone 17 Pro 应为 12GB（对站上实测值）")
    pro = next((x for x in data if "iPad Pro 13" in x["model"] and "M5" in x["model"]), None)
    ok(pro and len(pro["variants"]) == 2,
       "★ iPad Pro M5 有两个 RAM 档（12G/16G），不能压成一个")

filler = (ROOT / "app/agents/spec_filler.py").read_text(encoding="utf-8")
ok("_fill_apple" in filler and "apple_specs.json" in filler,
   "★ 苹果规格要接进规格补全 Agent，新产品才能自动享受，不能只是一次性脚本")
ok("_revoke_stale_inferences" in filler,
   "★ 每轮要撤销失效的 RAM 推断 —— 推断前提是「观测 ROM == 产品 ROM」，"
   "而产品规格会被后续补全改动，先前推的就悬空了（实测造成 60 条错误规格）")
ok(filler.index("_revoke_stale_inferences()") < filler.index("_infer_same_variant()"),
   "★ 顺序不能反：先撤销失效推断再重推，反过来会把刚推好的撤掉")

src = (ROOT / "app/scraping/applespec.py").read_text(encoding="utf-8")
from app.scraping.applespec import _JS  # noqa: E402

# 占位符在源码里，要看拼装后的 JS
ok("LPDDR" + chr(92) + "d*X?" in _JS,
   "★ 正则要吃掉 LPDDR 的版本号 —— 「8G LPDDR5128/256/512G」里的 5 属于 "
   "LPDDR5，不吃掉会解析出 5128 这种不存在的容量")
ok("__D__" not in _JS and "__SP__" not in _JS and "__NL__" not in _JS,
   "★ JS 拼装后不能残留占位符（本机 heredoc 会吞转义符，故用占位符写）")
ok("robots" in src, "合规依据要写在模块里（该站无 robots.txt）")

print(f"applespec: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
