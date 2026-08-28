# -*- coding: utf-8 -*-
"""GSMArena 规格源：手机 / 平板 / 智能手表。

★ 三条硬约束（都来自站点自己公开的条款，见 __init__.py）：
  1. **不碰 /res.php3 搜索端点** —— robots.txt 对所有 UA 禁止。
     改为走「品牌列表页 → 机型详情页」，列表页与详情页均未被禁止。
  2. **只取我们在跟踪的机型**，不做全库镜像。
     站方 RSL 许可写着 "restrict value extraction"，
     而我们真正需要的也只是价格库里那几百个型号的规格。
  3. **限速 + 落缓存**：默认 6 秒一个请求（站方给爬虫的 crawl-delay 是 5~20 秒），
     取到的原始规格进 spec_cache，重跑不会再打一次站点。

不用 Selenium 的原因：实测纯 HTTP 就能拿到（无 Cloudflare 挑战，
详情页规格写在 `data-spec` 属性里）。少开一个浏览器对我们和对方都更轻。
真遇到挑战页时上层可以换 Selenium 引擎（classify_block 已能识别）。
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("gsmarena")

BASE = "https://www.gsmarena.com/"
ATTRIBUTION = "GSMArena.com (CC BY 4.0)"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 我们跟踪的品牌 → GSMArena 的品牌列表页。
# 值取自 /makers.php3（品牌 id 是站点内部编号，不能猜）。
BRAND_SLUG = {
    "samsung": "samsung-phones-9.php",
    "apple": "apple-phones-48.php",
    "xiaomi": "xiaomi-phones-80.php",
    "honor": "honor-phones-121.php",
    "oppo": "oppo-phones-82.php",
    "vivo": "vivo-phones-98.php",
    "motorola": "motorola-phones-4.php",
    "acme": "acme-phones-58.php",
    "lenovo": "lenovo-phones-73.php",
    "asus": "asus-phones-46.php",
    "acer": "acer-phones-59.php",
    "hp": "hp-phones-41.php",
    "dell": "dell-phones-61.php",
    "sony": "sony-phones-7.php",
    "zte": "zte-phones-62.php",
    "tcl": "tcl-phones-123.php",
    "google": "google-phones-107.php",
    "realme": "realme-phones-118.php",
    "infinix": "infinix-phones-119.php",
    "nothing": "nothing-phones-128.php",
}


# ---------------------------------------------------------------- 名字归一

# 型号名匹配用的归一化：两边都压成"只剩字母数字"。
# ★ 不能只 lower()：我们库里是 "Galaxy A57"、站点是 "Galaxy A57"，看着一样，
#   但 "Galaxy S25+" / "Galaxy S25 Plus"、"Watch8" / "Watch 8" 这类写法差异
#   会让完全相同的机器对不上。空格与 + 号必须一并处理。
_PLUS = re.compile(r"\s*\+\s*|\bplus\b", re.I)


def normalize_model_key(name: str) -> str:
    s = (name or "").lower().strip()
    s = _PLUS.sub(" plus ", s)
    s = re.sub(r"\b5g\b|\b4g\b|\blte\b|\bdual\s*sim\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


# 型号名尾巴上常粘着的**规格/营销词**（不是型号的一部分）。
# 实测缺规格的手机里绝大多数是这么来的：
#   "A60 Ai Camera" / "A5 Pro IP69" / "A5 IP65 Ai" / "600 8 +"
# 这些词一律出现在型号**之后**，所以从右往左逐段砍就能露出真型号。
_TAIL_JUNK = re.compile(
    r"(?i)^(?:ip\d{2}|ipx\d|ai|ia|camera|c[áa]mara|nfc|dual|sim|esim|"
    r"\d{1,2}\s*(?:gb|ram|rom)?|\+|plus|pro\s*\+|5g|4g|lte|global|"
    r"bateria|battery|pantalla|display|smartphone|celular|libre|"
    r"desbloqueado|nuevo|oled|amoled|hz|mah|mp)$")


def trimmed_variants(model: str) -> list[str]:
    """从右往左逐段砍尾巴，**长的排前面**。

    ★ 这样做是安全的，因为上层只接受**精确命中**索引键：
      "A5 Pro IP69" → 先试 "A5 Pro IP69"（没有）→ "A5 Pro"（命中，停）
      永远不会掉到 "A5"（那是另一款机器）。
      如果反过来先试短的，A5 Pro 就会被错配成 A5 —— 两款不同价位的机器。
    """
    words = (model or "").split()
    out = []
    for n in range(len(words), 0, -1):
        cand = " ".join(words[:n])
        if n < len(words) and not _TAIL_JUNK.match(words[n]):
            # 被砍掉的那个词不是已知的规格/营销词 —— 不敢砍，停
            break
        out.append(cand)
    return out


def name_variants(model: str, brand: str = "") -> list[str]:
    """同一款货在两边的常见写法差异，逐个生成候选。

    ★ 这些不是"模糊匹配"，是**已知的等价写法**，一一对应，不会张冠李戴：
      · 苹果手表我们存 "Watch S5"，站点写 "Watch Series 5"
        （我们的短名来自电商标题，电商就爱写 S5）
      · 站点会把 "Watch Ultra 2" 写成 "Watch Ultra (2nd gen)"
      · 电商标题常带 5G/4G 后缀，站点分开建条目
    模糊匹配（编辑距离之类）在这里是危险的：Galaxy A56 与 A55 只差一个字符，
    但那是两款不同价位的机器 —— 宁可匹配不上，也不能匹配错。
    """
    m = (model or "").strip()
    if not m:
        return []
    out = [m]
    # Apple Watch: S5 / S9 → Series 5 / Series 9
    v = re.sub(r"(?i)\bwatch\s+s\s*(\d{1,2})\b", r"Watch Series \1", m)
    if v != m:
        out.append(v)
    # "Series 5" ↔ "S5" 反向也试一次
    v2 = re.sub(r"(?i)\bwatch\s+series\s*(\d{1,2})\b", r"Watch S\1", m)
    if v2 != m:
        out.append(v2)
    # 第几代：站点写 (2nd gen)
    v3 = re.sub(r"(?i)\b(\d)\s*$", r"(\1nd gen)", m)
    if v3 != m:
        out.append(v3)
    return out


class GsmArenaSource:
    """按需取规格。实例化一次，复用连接与限速状态。"""

    def __init__(self, delay: float = 6.0, timeout: float = 30.0, engine=None):
        # engine: 传入 ScrapeEngine 就走浏览器（Selenium/Playwright），否则纯 HTTP。
        # ★ 默认纯 HTTP 是有依据的，不是图省事：实测 289 个页面 0 失败 0 限流，
        #   该站没有 Cloudflare，规格就写在服务端 HTML 的 data-spec 属性里。
        #   浏览器在这里唯一的作用是慢 5~10 倍、内存高一个量级。
        #   保留这个入口是为了**万一站方哪天加了挑战** —— 那时切过来即可。
        self.engine = engine
        # 站方给具名爬虫的 crawl-delay 是 5~20 秒，这里取 6 秒起步。
        # 用户说过"扒几天都行"，所以宁可慢，不要给对方压力。
        self.delay = max(3.0, float(delay))
        self._last = 0.0
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.stats = {"fetched": 0, "cached": 0, "failed": 0, "blocked": 0}

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------ 取页

    def _get(self, url: str) -> str | None:
        """限速取页。被拦时返回 None 并计数，不重试轰炸。"""
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

        # 走浏览器（用户指定 --engine selenium 时）
        if self.engine is not None:
            try:
                text, html = self.engine.fetch(url)
            except Exception as e:  # noqa: BLE001
                log.warning("[gsmarena] 浏览器取页失败 %s: %s", url[:80], str(e)[:100])
                self.stats["failed"] += 1
                return None
            if not html:
                self.stats["failed"] += 1
                return None
            self.stats["fetched"] += 1
            return html

        try:
            r = self._client.get(url)
        except Exception as e:  # noqa: BLE001
            log.warning("[gsmarena] 请求失败 %s: %s", url[:80], str(e)[:100])
            self.stats["failed"] += 1
            return None
        if r.status_code == 429 or r.status_code == 503:
            # 明确的限流信号：退避，不要继续压
            log.warning("[gsmarena] 被限流 HTTP %s，退避 60 秒", r.status_code)
            self.stats["blocked"] += 1
            time.sleep(60)
            return None
        if r.status_code != 200:
            self.stats["failed"] += 1
            return None
        self.stats["fetched"] += 1
        return r.text

    # ------------------------------------------------ 品牌索引

    def brand_index(self, brand: str, max_pages: int = 40) -> dict[str, str]:
        """走品牌列表页，建 {归一化型号名: 详情页URL}。

        ★ 只读列表页（型号名 + 链接），不读规格 —— 规格只在真正需要时才逐个取。
        分页形态：首页 samsung-phones-9.php，之后 samsung-phones-f-9-0-p2.php。
        """
        slug = BRAND_SLUG.get((brand or "").lower())
        if not slug:
            return {}
        m = re.match(r"([a-z0-9]+)-phones-(\d+)\.php", slug)
        if not m:
            return {}
        name, bid = m.group(1), m.group(2)

        out: dict[str, str] = {}
        for page in range(1, max_pages + 1):
            url = urljoin(BASE, slug if page == 1
                          else f"{name}-phones-f-{bid}-0-p{page}.php")
            html = self._get(url)
            if not html:
                break
            found = self._parse_listing(html)
            if not found:
                break
            before = len(out)
            for model, href in found.items():
                out.setdefault(model, href)
            # 翻到没有新东西就停：分页越界时站点会回吐最后一页
            if len(out) == before:
                break
        log.info("[gsmarena] %s 索引 %d 个机型", brand, len(out))
        return out

    @staticmethod
    def _parse_listing(html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "lxml")
        out: dict[str, str] = {}
        for a in soup.select("div.makers a[href]"):
            href = a.get("href", "")
            span = a.find("span")
            if not href.endswith(".php") or span is None:
                continue
            model = span.get_text(" ", strip=True)
            if not model:
                continue
            out[normalize_model_key(model)] = urljoin(BASE, href)
        return out

    # ------------------------------------------------ 详情页

    def device_specs(self, url: str) -> dict | None:
        html = self._get(url)
        if not html:
            return None
        specs = parse_device_page(html)
        if specs:
            specs["source_url"] = url
            specs["attribution"] = ATTRIBUTION
        return specs


# ---------------------------------------------------------------- 解析

def parse_device_page(html: str) -> dict | None:
    """把机型详情页解析成结构化规格。

    规格写在 `data-spec` 属性里，比逐个找表格行稳得多。
    """
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return None

    raw: dict[str, str] = {}
    for el in soup.select("[data-spec]"):
        key = el.get("data-spec")
        val = el.get_text(" ", strip=True)
        if key and val:
            raw.setdefault(key, val)
    if not raw.get("modelname"):
        return None

    out: dict = {
        "model_name": raw.get("modelname"),
        "raw": raw,
        "chipset": _clean_chipset(raw.get("chipset")),
        "os": _first_clause(raw.get("os")),
        "screen_size": _screen_inches(raw.get("displaysize")),
        "screen_tech": _screen_tech(raw.get("displaytype")),
        "battery_mah": _battery(raw.get("batdescription1") or raw.get("battery")
                                or raw.get("batdescription")),
        "camera_main_mp": _camera_mp(raw.get("cam1modules")),
        "launch_date": _launch_date(raw.get("year") or raw.get("released")),
        "memory_options": _memory_options(raw.get("internalmemory")),
    }
    ram, rom = _pick_memory(out["memory_options"])
    out["ram_gb"], out["rom_gb"] = ram, rom
    return out


def _first_clause(v: str | None) -> str | None:
    if not v:
        return None
    return re.split(r"[,;]", v)[0].strip()[:80] or None


def _clean_chipset(v: str | None) -> str | None:
    """芯片名里带着制程和地区差异，截到主名即可。

    "Qualcomm SM8650-AC Snapdragon 8 Gen 3 (4 nm) - USA/Canada/China Exynos"
      → "Qualcomm SM8650-AC Snapdragon 8 Gen 3"
    """
    if not v:
        return None
    s = re.split(r"\s*[-–]\s*(?:USA|International|Global|EMEA)", v)[0]
    s = re.sub(r"\(\s*\d+\s*nm\s*\)", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:80] or None


def _screen_inches(v: str | None) -> float | None:
    if not v:
        return None
    m = re.search(r"([\d.]+)\s*inch", v, re.I)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _screen_tech(v: str | None) -> str | None:
    if not v:
        return None
    for t in ("LTPO AMOLED", "Dynamic AMOLED", "Super AMOLED", "AMOLED",
              "OLED", "Retina", "IPS LCD", "PLS LCD", "TFT", "LCD"):
        if t.lower() in v.lower():
            return t
    return _first_clause(v)


def _battery(v: str | None) -> int | None:
    if not v:
        return None
    m = re.search(r"([\d,]{3,6})\s*mAh", v, re.I)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _camera_mp(v: str | None) -> float | None:
    """主摄像素：取第一个模组的 MP（cam1modules 里第一段就是主摄）。"""
    if not v:
        return None
    m = re.search(r"([\d.]+)\s*MP", v, re.I)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _launch_date(v: str | None) -> str | None:
    """"2024, January 17" / "Released 2024, January 24" → 2024-01-17"""
    if not v:
        return None
    m = re.search(r"(\d{4})\s*,\s*([A-Za-z]+)(?:\s+(\d{1,2}))?", v)
    if not m:
        m2 = re.search(r"\b(\d{4})\b", v)
        return f"{m2.group(1)}-01-01" if m2 else None
    year, mon, day = m.group(1), m.group(2)[:3].lower(), m.group(3)
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    mm = months.get(mon)
    if not mm:
        return f"{year}-01-01"
    return f"{year}-{mm:02d}-{int(day or 1):02d}"


def _memory_options(v: str | None) -> list[dict]:
    """"128GB 8GB RAM, 256GB 12GB RAM" → [{rom:128, ram:8}, {rom:256, ram:12}]

    ★ 一款机器有多个存储配置，**不能只留一个**：
      竞品匹配比的是"同配置"，把 512GB 版当成 128GB 版比价会差出一大截。
      这里全存下来，选用时再挑。
    """
    if not v:
        return []
    out = []
    for part in v.split(","):
        rom = re.search(r"([\d.]+)\s*(GB|TB|MB)\b", part, re.I)
        ram = re.search(r"([\d.]+)\s*GB\s*RAM", part, re.I)
        if not rom:
            continue
        try:
            size = float(rom.group(1))
        except ValueError:
            continue
        unit = rom.group(2).upper()
        rom_gb = size * 1024 if unit == "TB" else (size / 1024 if unit == "MB" else size)
        item = {"rom_gb": int(rom_gb)}
        if ram:
            try:
                item["ram_gb"] = int(float(ram.group(1)))
            except ValueError:
                pass
        out.append(item)
    return out


def _pick_memory(options: list[dict]) -> tuple[int | None, int | None]:
    """挑一个代表配置：取**最低配**。

    理由：拉美在售的以低配为主，且低配是各家都有的档位，跨品牌可比性最好。
    取最高配会系统性抬高我们对友商的规格印象。
    """
    if not options:
        return None, None
    best = min(options, key=lambda o: (o.get("rom_gb") or 0, o.get("ram_gb") or 0))
    return best.get("ram_gb"), best.get("rom_gb")
