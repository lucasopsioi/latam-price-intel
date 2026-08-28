# -*- coding: utf-8 -*-
"""品类交叉校验（采集上下文 vs 标题证据）—— 2026-08-27。

事故：price_obs.category_code 存的是「当时在抓哪个品类页」，不是商品本身的品类
（collector._persist 直接把采集单元的 category 写进去）。平板桶里因此躺着
339 MXN 的「XIAOMI Audífonos Buds 6 Play」、68,000 COP 的「Reloj Inteligente
Smartwatch T900」—— 分类器判 device 是**对的**（确实是台设备），错的只有品类。
后果：平板价格下沿被拽穿（MXN 品类 P5 从 1,449 掉到 799）。

规则：extract.crosscheck_category —— 本品类**毫无**证据 + 他类有明确证据 才动。

★ 本文件的重点不是"能不能认出耳机"，而是**闸门**：
  下面每一条 ok_ 用例都对应一个实测会被改错的成簇形态（赠品/捆绑/兼容/卖家名）。
  松开 _CAT_CUT 里任何一个词，它们就会成片地被判错，而且不报错。

跑法： python tests\test_categorycrosscheck.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scraping import extract             # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def cc(title, ctx):
    """(结论, 目标) —— fix 时把目标拼进去，方便一行断言"""
    v, tgt, _ = extract.crosscheck_category(title, ctx)
    return f"{v}/{tgt}" if v == "fix" else v


print("== 用户实测样本：平板桶里的真设备、错品类 ==")
check("Buds 6 Play（339 MXN Liverpool）",
      cc("XIAOMI Audífonos In-Ear Buds 6 Play inalámbricos", "tablet"), "fix/audio")
check("Redmi Buds 8 Lite（399 MXN）",
      cc("XIAOMI Audífonos true wireless redmi buds 8 lite", "tablet"), "fix/audio")
check("Redmi Buds 6 Play（439 MXN Sanborns）",
      cc("Audífonos Xiaomi Redmi Buds 6 Play Bluetooth", "tablet"), "fix/audio")
check("T900 智能表（68,000 COP Falabella CO）",
      cc("Reloj Inteligente Smartwatch T900 Pro Max L Gratis Audifonos", "tablet"),
      "fix/wearable")
check("Winfun 玩具平板（49,900 COP，是 COP 的下沿）",
      cc("Juguete para bebé didáctico Winfun: Tablet I-Fun pad interactiva", "tablet"),
      "pending")

print("== ★ 闸门①：赠品 —— 送的东西不是本商品 ==")
# 不加闸实测 77+48 条手机被判成音频
check("手机送耳机（Gratis 之后的 audifonos 不算数）",
      cc("Honor 600E 512GB 5G Gratis Honor Play10+audifonos", "phone"), "ok")
check("手机送 buds+音箱",
      cc("Vivo Y11D 256Gb 4G Gratis Buds+Speaker", "phone"), "ok")
check("手机送智能表",
      cc("vivo V70 512GB 5G Gratis Smartwatch", "phone"), "ok")
check("Bundle 也是赠品标记（Vega 70 是手机，不是 Sonicbuds）",
      cc("ACME Acme Vega 70 Bundle Sonicbuds Pro 3 OLED 6.6 pulgadas MVNO", "phone"),
      "ok")
check("+ 之后的配件不算数",
      cc("HONOR 600e 5G REGISTRADO 8RAM 256GB DESERT GOLD + EARBUDS X7 LITE", "phone"),
      "ok")
check("手机 con 平板（con 也是闸）",
      cc("Xiaomi 17T 512GB 5G con Redmi Pad+Xiaomi A7 64GB", "phone"), "ok")

print("== ★ 闸门②：兼容/依附 —— para 之后的设备名是「配给谁用」，不是「这是什么」 ==")
check("iPad 键盘（配件，不是平板）",
      cc("APPLE Magic Keyboard para iPad Pro 13 pulgadas M4", "audio"), "ok")
check("手机三脚架（不是手机）",
      cc("170 cm Trípode para Celular, Selfie Stick Extensible Bluetooth", "wearable"),
      "ok")
check("笔记本延保（连实物都不是）",
      cc("Garantía Extendida para Laptop 12 Meses", "phone"), "ok")
check("Slate Tab 触控笔",
      cc("M-Pencil 3rd Gen para Slate Tab", "phone"), "ok")
check("智能表配 iPhone 用",
      cc("Smart Watch 8 Ultra Para IPhone Android IOS. Breca Bazar", "wearable"), "ok")

print("== ★ 闸门③：卖家名 —— 「Por <卖家>」尾巴会注入假证据 ==")
# 知识页原话：「有个卖家就叫 Cablex，cable 子串一命中整台手机被判成配件」
check("Por 之后不取证据（且前面的 audifonos 照常取到）",
      cc("Envío gratis app HONOR Audífonos In Ear Inalámbricos Bluetooth 5.3 Honor "
         "Por Water.CL", "wearable"), "fix/audio")

print("== ★★ 闸门与渠道角标的冲突：Envío gratis **app** 里的 gratis 是同一个词 ==")
# 不先剥角标，_CAT_CUT 会在第 6 个字符切掉，658 条真耳机全部漏判
check("Falabella 配送角标不得截断头部",
      cc("Envío gratis app XIAOMI Audífonos Redmi Buds 6", "tablet"), "fix/audio")
check("Paris/Ripley 三层角标同理",
      cc("Vista Previa Recíbelo hoy BOSE AUDÍFONOS BOSE IN EAR QUIETCOMFORT", "wearable"),
      "fix/audio")

print("== ★ 同形词：西语 tableta 复数是「药片」 ==")
check("保健品 60 tabletas 不是平板",
      cc("SALUD NATURAL Caltrón 600+D de 60 tabletas", "phone"), "ok")
check("单数 tableta 是平板",
      cc("Tableta Samsung Galaxy Tab A9 64GB", "audio"), "fix/tablet")

print("== 本品类有证据 → 一律不动（捆绑装靠这条自动成立）==")
check("平板 + 送键盘壳 仍是平板",
      cc('Tablet Samsung Galaxy Tab S11 Ultra 14.6" 256GB + Cover', "tablet"), "ok")
check("平板 + 送耳机 仍是平板",
      cc("Tablet Lenovo Idea Tab 128GB Incluye Protector y Audífonos", "tablet"), "ok")
check("耳机在音频桶里，本来就对",
      cc("Audífonos Sudio A3 PRO Inalambricos Blancos", "audio"), "ok")

print("== 判不出就不猜 ==")
check("无任何品类词（型号名裸奔）",
      cc("Xiaomi 14T 256GB 12GB RAM Negro", "phone"), "ok")
check("没有采集品类时不判",
      cc("Audífonos Xiaomi Redmi Buds 6", None), "ok")
check("空标题",
      cc("", "tablet"), "ok")

print("== 多类证据 → 待定，不硬选 ==")
check("音箱+耳机+带屏设备的三合一组合",
      cc("Xiaomi Sound Pocket Bocina Portátil Audífonos pantalla táctil Future Pod", "phone"),
      "pending")

print("== 真实改判方向（各品类至少钉一条）==")
check("Fast Shop 巴西手机躺在平板桶",
      cc("Celular OPPO A6X Azul, 128GB, 4GB RAM e Câmera Traseira de 13MP", "tablet"),
      "fix/phone")
check("笔记本躺在平板桶",
      cc("Notebook Gamer LOQ Intel Core i7-13645HX 16GB RAM 512GB SSD RTX 5050", "tablet"),
      "fix/pc")
check("iPad 躺在 PC 桶",
      cc("Apple iPad 11 ROSA 2026", "pc"), "fix/tablet")
check("智能表躺在手机桶",
      cc("Smartwatch Samsung Watch 8 40 Mm Resistente Al Agua Gris", "phone"),
      "fix/wearable")
check("iPhone 躺在穿戴桶",
      cc("iPhone 17 Pro Max (2TB) Laranja-cósmico, Tela de 6,9", "wearable"), "fix/phone")

print("== ★ 定点回归：Lenovo Legion Tab / Yoga Tab 是**游戏平板**，不是笔记本 ==")
# skunorm.guess_category 按 ^legion / ^yoga 判成 pc（那是笔记本产品线名），
# 而标题白纸黑字写着 Tablet —— 这 20 条分歧里标题是对的一方。
check("Legion Tab 判平板",
      cc('LENOVO Tablet Lenovo Legion Tab 8.8" 256GB 12GB RAM Eclipse Black', "pc"),
      "fix/tablet")
check("Yoga Tab Plus 判平板",
      cc('LENOVO Tablet Lenovo Yoga Tab Plus 12.7" 256GB 16GB RAM', "audio"),
      "fix/tablet")

print("== category_evidence 的位置序（谁先出现谁说了算）==")
ev = extract.category_evidence("Reloj Inteligente Smartwatch T900 Gratis Audifonos")
check("赠品段被闸掉后只剩穿戴证据", [c for _, c, _ in ev], ["wearable"])
ev2 = extract.category_evidence("Tableta Samsung Galaxy S6 Lite", head_only=False)
check("同时命中时按位置升序", [c for _, c, _ in ev2][0], "tablet")

print(f"\n{PASS} pass / {FAIL} fail")
sys.exit(1 if FAIL else 0)
