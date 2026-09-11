# -*- coding: utf-8 -*-
"""外部规格源测试（GSMArena）。

全部用**离线样本**跑，不联网：
  测试联网就会变成"网络一抖测试就红"，而且每跑一次都在打人家的站。

★ 这个文件盯三件事：
  1. 解析正确 —— 规格数字错了不会报错，只会让竞品匹配悄悄比错
  2. 型号名对齐 —— 两边写法差异（S25+ / S25 Plus、Watch S5 / Watch Series 5）
     对不上就是"有数据也用不上"
  3. **不许模糊匹配** —— Galaxy A56 与 A55 只差一个字符却是两款不同价位的机器，
     宁可匹配不上，也不能匹配错

跑法： python tests\test_specsource.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scraping.specsource import (name_variants,  # noqa: E402
                                     normalize_model_key, parse_device_page)
from app.scraping.specsource.gsmarena import (_battery, _camera_mp,  # noqa: E402
                                              _clean_chipset, _launch_date,
                                              _memory_options, _pick_memory,
                                              _screen_inches, _screen_tech)

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def check_true(name, cond, hint=""):
    check(f"{name}{(' — ' + hint) if hint else ''}", bool(cond), True)


print("== 单字段解析 ==")
# 芯片名带制程和地区差异，截到主名
check("芯片-去制程与地区",
      _clean_chipset("Qualcomm SM8650-AC Snapdragon 8 Gen 3 (4 nm) - USA/Canada/China Exynos"),
      "Qualcomm SM8650-AC Snapdragon 8 Gen 3")
check("芯片-简单情形", _clean_chipset("Mediatek Dimensity 6300"), "Mediatek Dimensity 6300")
check("芯片-空", _clean_chipset(None), None)

check("屏幕英寸", _screen_inches("6.2 inches, 94.4 cm2 (~90.9% screen-to-body ratio)"), 6.2)
check("屏幕英寸-无", _screen_inches("Foldable"), None)
check("屏幕技术", _screen_tech("Dynamic LTPO AMOLED 2X, 120Hz, HDR10+"), "LTPO AMOLED")
check("屏幕技术-LCD", _screen_tech("IPS LCD, 90Hz"), "IPS LCD")

check("电池", _battery("Li-Ion 4000 mAh, non-removable"), 4000)
check("电池-带千分位", _battery("Li-Po 10,200 mAh"), 10200)
check("电池-无", _battery("Removable"), None)

# 主摄取第一个模组的 MP
check("主摄像素", _camera_mp("50 MP, f/1.8, 24mm (wide) 10 MP, f/2.4 12 MP, f/2.2"), 50.0)
check("主摄-无", _camera_mp("Yes"), None)

print("== ★ 首发日期：错了会让上市看板的滞后天数全算错 ==")
check("完整日期", _launch_date("2024, January 17"), "2024-01-17")
check("带 Released 前缀", _launch_date("Available. Released 2024, January 24"), "2024-01-24")
check("只有年月", _launch_date("2025, March"), "2025-03-01")
check("只有年份", _launch_date("Exp. release 2026"), "2026-01-01")
check("空", _launch_date(None), None)

print("== ★ 存储配置：必须全存，不能只留一个 ==")
# 同一款机器多个存储档是**不同价位**，只留一个会拿 512G 版去和 128G 版比价
opts = _memory_options("128GB 8GB RAM, 256GB 8GB RAM, 256GB 12GB RAM, 512GB 8GB RAM")
check("配置条数", len(opts), 4)
check("首个配置", opts[0], {"rom_gb": 128, "ram_gb": 8})
check("TB 换算", _memory_options("1TB 16GB RAM")[0], {"rom_gb": 1024, "ram_gb": 16})
check("无 RAM 信息", _memory_options("64GB")[0], {"rom_gb": 64})
check("空", _memory_options(None), [])
# 代表配置取**最低配**：拉美在售以低配为主，取高配会系统性抬高对友商的规格印象
check("代表配置取最低", _pick_memory(opts), (8, 128))
check("无配置", _pick_memory([]), (None, None))

print("== ★ 型号名对齐（对不上 = 有数据也用不上）==")
check_true("空格差异", normalize_model_key("Galaxy Watch8") == normalize_model_key("Galaxy Watch 8"))
check_true("加号与 Plus", normalize_model_key("Galaxy S25+") == normalize_model_key("Galaxy S25 Plus"))
check_true("5G 后缀", normalize_model_key("Redmi Note 15 5G") == normalize_model_key("Redmi Note 15"))
check_true("大小写", normalize_model_key("IPHONE 15 PRO") == normalize_model_key("iPhone 15 Pro"))

print("== ★★ 绝不许模糊匹配：一个字符之差是两款不同价位的机器 ==")
check_true("A56 ≠ A55", normalize_model_key("Galaxy A56") != normalize_model_key("Galaxy A55"))
check_true("S24 ≠ S24 Ultra",
           normalize_model_key("Galaxy S24") != normalize_model_key("Galaxy S24 Ultra"))
check_true("iPhone 15 ≠ 15 Pro",
           normalize_model_key("iPhone 15") != normalize_model_key("iPhone 15 Pro"))
check_true("Note 15 ≠ Note 15 Pro",
           normalize_model_key("Redmi Note 15") != normalize_model_key("Redmi Note 15 Pro"))

print("== 已知等价写法（一一对应，不是模糊）==")
# 电商标题爱写 "Watch S5"，站点写 "Watch Series 5"
check_true("Apple Watch S5 → Series 5",
           "Watch Series 5" in name_variants("Watch S5"))
check_true("Watch Series 9 → S9", "Watch S9" in name_variants("Watch Series 9"))
check_true("普通型号不生成多余变体", name_variants("Galaxy A57") == ["Galaxy A57"])
check("空输入", name_variants(""), [])

print("== 整页解析（离线样本）==")
SAMPLE = """
<html><body>
<h1 data-spec="modelname">Samsung Galaxy S24</h1>
<td data-spec="year">2024, January 17</td>
<td data-spec="chipset">Qualcomm SM8650-AC Snapdragon 8 Gen 3 (4 nm)</td>
<td data-spec="displaysize">6.2 inches, 94.4 cm2</td>
<td data-spec="displaytype">Dynamic LTPO AMOLED 2X, 120Hz</td>
<td data-spec="internalmemory">128GB 8GB RAM, 256GB 12GB RAM</td>
<td data-spec="batdescription1">Li-Ion 4000 mAh, non-removable</td>
<td data-spec="cam1modules">50 MP, f/1.8, 24mm (wide)</td>
<td data-spec="os">Android 14, up to 7 major OS updates</td>
</body></html>
"""
s = parse_device_page(SAMPLE)
check_true("解析成功", s is not None)
check("型号名", s["model_name"], "Samsung Galaxy S24")
check("芯片", s["chipset"], "Qualcomm SM8650-AC Snapdragon 8 Gen 3")
check("屏幕", s["screen_size"], 6.2)
check("电池", s["battery_mah"], 4000)
check("主摄", s["camera_main_mp"], 50.0)
check("首发", s["launch_date"], "2024-01-17")
check("代表 RAM/ROM", (s["ram_gb"], s["rom_gb"]), (8, 128))
check("存储配置全留", len(s["memory_options"]), 2)
check("OS 只取主干", s["os"], "Android 14")

# 没有 modelname 的页面不是机型页 —— 必须返回 None 而不是半个空壳
check("非机型页返回 None", parse_device_page("<html><body>404</body></html>"), None)
check("空输入返回 None", parse_device_page(""), None)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
