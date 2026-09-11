# -*- coding: utf-8 -*-
"""苹果产品规格源：hubweb.cn（用户 2026-09-03 指定）。

为什么单独给苹果做一个源：
  苹果**从不在商品标题里标 RAM**（"iPhone 17 Pro Max 256GB" 不写 12GB），
  拉美渠道的列表页也没有，所以标题解析永远拿不到。而没有 RAM 就没法做
  同配置比价 —— 用户原话：「对于手机、平板来说，没有 RAM/ROM 都没有可比性」。

★★ 苹果的关键性质（这一点让填充变安全）：**同一机型的 RAM 不随存储变化**。
   iPhone 17 Pro 无论 256G/512G/1TB 都是 12GB。安卓阵营不成立
   （Galaxy A17 的 128G 版是 4GB、256G 版是 8GB），所以那边只能同变体推断。
   ⇒ 苹果可以按**机型**填 RAM，不需要知道买的是哪个存储版本。

站点结构（2026-09-03 实测）：
  - 每个机型是一个 <li>，机型名是 li 内第一行文本
  - RAM 在 li 内 .li-div-npu 元素里，形如 "12GB LPDDR5X256GB、512GB、1TB"
  - 同一机型会出现两次（名称行 + 参数行），按机型名去重
  - iPhone 一页；iPad 分 pro/air/ipad/mini 四页

合规：hubweb.cn 无 robots.txt（404，未声明限制），是公开参数库、无登录墙，
      单次全量只 5 个页面请求。
"""
from __future__ import annotations

import logging
import re
import time

log = logging.getLogger("applespec")

# 反斜杠常量：本机的 shell heredoc 会吞掉 \n / \d 这类转义（踩过多次），
# 所以 JS 里的正则用占位符写、在这里拼回去。
BS = chr(92)

BASE = "https://hubweb.cn/apple-device"
PAGES = [
    ("phone", f"{BASE}/iphone/", r"^iPhone"),
    ("tablet", f"{BASE}/ipad/ipad-pro/", r"^iPad"),
    ("tablet", f"{BASE}/ipad/ipad-air/", r"^iPad"),
    ("tablet", f"{BASE}/ipad/ipad/", r"^iPad"),
    ("tablet", f"{BASE}/ipad/ipad-mini/", r"^iPad"),
]

# 页面结构（2026-09-03 实测，iPhone 与 iPad 同类名不同写法）：
#   每个机型一个 <li>，机型名是 li 内前两行文本（iPad 的代次在第二行）
#   每个 .li-div-npu 是一个 RAM 档，形如 "12G LPDDR5X256G、512G"
#   ★ iPad Pro 的 RAM **随容量变化**（256/512G→12G，1T/2T→16G），
#     所以一个机型可能有多个 .li-div-npu，必须按容量档分别记。
#   ★ 三个真实坑：
#     ① iPhone 写 "8GB"，iPad 写 "8G" —— 单位可有可无
#     ② 必须吃掉 LPDDR 的版本号：「8G LPDDR5128/256/512G」里的 5 属于
#        LPDDR5，不吃掉会解析出 5128 这种不存在的容量
#     ③ 容量可能写成 "128/256/512G"，单位只在末尾 —— 要向后继承
_JS = r"""
(() => {
  function parseCaps(s) {
    const toks = s.split(/[、,，/]+/).map(x => x.trim()).filter(Boolean);
    let unit = 'G';
    for (let i = toks.length - 1; i >= 0; i--) {
      const u = toks[i].match(/(TB|T|GB|G)__DOLLAR__/i);
      if (u) { unit = u[1]; break; }
    }
    const out = [];
    for (const t of toks) {
      const m = t.match(/^(__D__+)__SP__*(TB|T|GB|G)?/i);
      if (!m) continue;
      const u = m[2] || unit;
      out.push(parseInt(m[1], 10) * (/^t/i.test(u) ? 1024 : 1));
    }
    return out;
  }
  const seen = new Set(), rows = [];
  for (const li of document.querySelectorAll('li')) {
    const npus = li.querySelectorAll('.li-div-npu');
    if (!npus.length) continue;
    const name = (li.innerText || '').trim().split('__NL__').slice(0, 2).join(' ').trim();
    if (!name || !/__RE__/i.test(name) || seen.has(name)) continue;
    const variants = [];
    for (const npu of npus) {
      const txt = npu.textContent || '';
      const m = txt.match(/(__D__+)__SP__*GB?__SP__*LPDDR__D__*X?/i);
      if (!m) continue;
      variants.push({
        ram: parseInt(m[1], 10),
        caps: parseCaps(txt.slice(txt.indexOf(m[0]) + m[0].length)),
      });
    }
    if (variants.length) { seen.add(name); rows.push({ name, variants }); }
  }
  return rows;
})()
"""
_JS = (_JS.replace("__D__", BS + "d").replace("__SP__", BS + "s")
          .replace("__NL__", BS + "n").replace("__DOLLAR__", BS + "s*$"))


# li 第一行常带页面附件链接文案：「iPhone 17 Pro 宣传图 / 内部图 1 / 内部图 2」
_TAIL = re.compile(r"\s*(宣传图|内部图|技术规格|维修手册|规格与手册)[\s/0-9]*.*$")


def clean_model(name: str) -> str:
    """去掉机型名后面粘连的页面文案，保留「iPhone 17 Pro」「iPad Pro 13 英寸 (M5)」。"""
    return _TAIL.sub("", (name or "").strip()).strip()


def _norm(name: str) -> str:
    """机型名归一：小写、去空格/连字符/中文括号，便于与库里的型号匹配。"""
    s = clean_model(name).lower()
    s = s.replace("英寸", "").replace("（", "(").replace("）", ")")
    return re.sub(r"[\s\-_]+", "", s)


def fetch_all(js_eval, sleep: float = 1.0) -> list[dict]:
    """抓全部苹果机型规格。

    js_eval(url, script) -> list —— 由调用方注入（浏览器引擎或工具），
    本模块不绑定具体驱动，方便单测与换引擎。
    ★ 页面间留间隔：5 个页面不算压力，但没必要贴着打。
    """
    out: list[dict] = []
    for cat, url, name_re in PAGES:
        try:
            rows = js_eval(url, _JS.replace("__RE__", name_re.lstrip("^"))) or []
        except Exception:                          # noqa: BLE001
            log.warning("苹果规格页抓取失败：%s", url, exc_info=True)
            continue
        for r in rows:
            variants = [v for v in (r.get("variants") or []) if v.get("ram")]
            if not r.get("name") or not variants:
                continue
            out.append({
                "category": cat,
                "model": clean_model(r["name"]),
                "key": _norm(r["name"]),
                # 每档：(容量列表, 该档 RAM)。iPhone 只有一档；
                # iPad Pro 有两档（256/512G→12G，1T/2T→16G）。
                "variants": [{"ram_gb": int(v["ram"]),
                              "caps": sorted({int(c) for c in (v.get("caps") or [])})}
                             for v in variants],
                "source": url,
            })
        log.info("苹果规格 %s：%d 款（%s）", cat, len(rows), url)
        time.sleep(sleep)
    return out


def ram_for(spec: dict, rom_gb: int | None) -> int | None:
    """按存储容量取该档的 RAM。

    ★ iPhone 只有一档 → 不需要知道容量，直接给（这正是苹果可安全填充的原因）。
    ★ iPad Pro 有多档（256/512G→12G，1T/2T→16G）→ **必须**知道容量；
      不知道容量时，只有当所有档的 RAM 相同才敢给，否则返回 None 不猜。
    """
    vs = spec.get("variants") or []
    if not vs:
        return None
    if len(vs) == 1:
        return vs[0]["ram_gb"]
    if rom_gb:
        for v in vs:
            if rom_gb in (v.get("caps") or []):
                return v["ram_gb"]
        return None                      # 容量对不上任何一档 → 不猜
    rams = {v["ram_gb"] for v in vs}
    return rams.pop() if len(rams) == 1 else None


_SERIES = (("ipadpro", "pro"), ("ipadair", "air"), ("ipadmini", "mini"),
           ("ipad", "base"))
_CHIP = re.compile(r"\b(m\d+|a\d+\s*pro|a\d+)\b", re.I)
_SIZE = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*(?:英寸|inch|\")?\b")
_GEN = re.compile(r"(?:第\s*(\d+)\s*代|(\d+)(?:st|nd|rd|th)\s*gen)", re.I)


def _facets(name: str) -> dict:
    """把机型名拆成可比对的要素：系列 / 尺寸 / 芯片 / 代次。

    库里与站上的写法差异大：
      "Apple iPad Air 11 M4"  ↔  "iPad Air 11 英寸 (M4)"
      "Apple iPad A16 (11th Gen)" ↔ "iPad (A16)"
      "Apple iPad 9th Gen"    ↔  "iPad (第 9 代)"
    直接做字符串匹配全都对不上，只能拆要素后按要素比。
    """
    # ★ 必须先剥 Apple 前缀：库里叫「Apple iPad Air 11 M4」，站上叫
    #   「iPad Air 11 英寸 (M4)」。不剥的话 series 认不出来（实测全军覆没）。
    raw = re.sub(r"^\s*apple\s+", "", clean_model(name), flags=re.I)
    k = _norm(raw)
    series = next((v for pre, v in _SERIES if k.startswith(pre)), None)
    # 尺寸只在系列词之后找，避免把 "iPad 9th" 的 9 当尺寸
    tail = raw
    for pre in ("iPad Pro", "iPad Air", "iPad mini", "iPad Mini", "iPad"):
        if raw.lower().startswith(pre.lower()):
            tail = raw[len(pre):]
            break
    chip = _CHIP.search(tail)
    gen = _GEN.search(raw)
    # ★ 代次要先摘掉再找尺寸，否则「第 9 代」「9th Gen」的 9 会被当成 9 英寸
    #   （实测 iPad 第 9 代被解析成 size=9.0，与真正的 11 英寸机型混淆）。
    size_src = _GEN.sub(" ", tail)
    size = None
    for m in _SIZE.finditer(size_src):
        v = float(m.group(1))
        if 7 <= v <= 14:                 # 平板尺寸的合理区间
            size = v
            break
    return {
        "series": series,
        "size": size,
        "chip": re.sub(r"\s+", "", chip.group(1)).lower() if chip else None,
        "gen": int(gen.group(1) or gen.group(2)) if gen else None,
    }


def match_ipad(db_model: str, specs: list[dict]) -> dict | None:
    """iPad 专用匹配：按 系列+芯片 或 系列+代次 对齐，尺寸用于消歧。

    ★ 宁缺勿错：要素不足以唯一确定时返回 None。
      "Apple iPad Air"（没芯片没代次没尺寸）会匹配到 10 个 Air，直接放弃 ——
      给它填个 RAM 等于瞎猜。
    """
    a = _facets(db_model)
    if not a["series"]:
        return None
    # ★ 没有任何区分要素（芯片/代次/尺寸）就不匹配，**与候选池里剩几个无关**。
    #   「Apple iPad Air」在现实里横跨 7 代、RAM 从 1G 到 12G；哪怕规格表里
    #   碰巧只剩一款，配上去也是碰运气。宁可留空。
    if a["chip"] is None and a["gen"] is None and a["size"] is None:
        return None
    cands = [s for s in specs if _facets(s["model"])["series"] == a["series"]]
    if not cands:
        return None
    # ★ 芯片优先、代次兜底 —— 两者**不能同时要求**：
    #   库里「Apple iPad A16 (11th Gen)」既有芯片 A16 又有代次 11，
    #   而站上的对应机型叫「iPad (A16)」根本没写代次，同时卡就永远匹配不上。
    #   芯片是更可靠的标识（同代次可能有多芯片版本，反之极少）。
    if a["chip"]:
        by_chip = [s for s in cands if _facets(s["model"])["chip"] == a["chip"]]
        if by_chip:
            cands = by_chip
        elif a["gen"] is not None:
            cands = [s for s in cands if _facets(s["model"])["gen"] == a["gen"]]
    elif a["gen"] is not None:
        cands = [s for s in cands if _facets(s["model"])["gen"] == a["gen"]]
    if not cands:
        return None
    if a["size"] is not None and len(cands) > 1:
        sized = [s for s in cands if _facets(s["model"])["size"] == a["size"]]
        if sized:
            cands = sized
    return cands[0] if len(cands) == 1 else None


def match_model(db_model: str, specs: list[dict]) -> dict | None:
    """把库里的型号名匹配到苹果规格。

    ★ 只接受**精确**或**唯一前缀**匹配。库里的名字五花八门
      （"iPhone 17 Pro Max"、"Apple iPhone 17 Pro Max"、"iPhone 17 Pro Max 256GB"），
      但绝不能把 "iPhone 17" 匹配到 "iPhone 17 Pro"（RAM 8 vs 12，差一档）。
      所以候选多于一个时**放弃**，宁可不填。
    """
    k = _norm(db_model)
    if not k:
        return None
    k = re.sub(r"^apple", "", k)
    exact = [s for s in specs if s["key"] == k]
    if len(exact) == 1:
        return exact[0]
    # 前缀匹配：库里名字可能带后缀（容量/颜色）。取最长的那个规格名，
    # 且要求唯一 —— "iphone17" 会同时前缀匹配 17/17pro/17promax，此时放弃。
    pref = [s for s in specs if k.startswith(s["key"])]
    if pref:
        pref.sort(key=lambda s: -len(s["key"]))
        best = pref[0]
        if sum(1 for s in pref if len(s["key"]) == len(best["key"])) == 1:
            return best
    return None
