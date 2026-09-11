# -*- coding: utf-8 -*-
"""情报源：新闻 RSS + 全球科技站点。

为什么用新闻源覆盖社媒（X / Instagram / YouTube）：
  这三家对自动化抓取封锁极严，直接爬网页版违反平台条款且极易封 IP，
  做出来也撑不了几天。而品牌在社媒发起的重要动作（发布会、新品、
  大促、代言）当地科技媒体几乎都会报道 —— 按国家/语言检索的新闻流
  能覆盖绝大部分"友商动态"，且完全合规、稳定。

  X 官方 API（付费套餐）填了 Bearer Token 就自动启用，作为补充。
  Instagram / Facebook 的 API 只开放给主页管理者本人，没有合规的
  第三方读取通道 —— 这一条是平台限制，不是技术难度。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from .browser import UA

log = logging.getLogger("news")

GOOGLE_NEWS = ("https://news.google.com/rss/search"
               "?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}")

# 全球 top 科技站点：新品首发几乎都在这些站最先出现。
# 情报 Agent 每天扫它们，发现"全球已发布但拉美还没上市"的产品 ——
# 这正是上市节奏看板需要的"全球首发日期"。
GLOBAL_TECH_FEEDS = [
    ("GSMArena", "https://www.gsmarena.com/rss-news-reviews.php3"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Engadget", "https://www.engadget.com/rss.xml"),
    ("Android Authority", "https://www.androidauthority.com/feed/"),
    ("NotebookCheck", "https://www.notebookcheck.net/News.152.0.html?type=100&feed=1"),
    ("9to5Mac", "https://9to5mac.com/feed/"),
    ("SamMobile", "https://www.sammobile.com/feed/"),
]

# 拉美本地科技媒体：新品在当地上市的第一手消息。
#
# ★ 每条是 (源名, URL, scope)：
#   scope="local"    该国本土媒体 → 文章可以标成这个国家
#   scope="regional" 泛拉美媒体（FayerWayer / Xataka 西语版）→ **不能**标成某国。
#     把区域文章记成"智利的动态"是伪造归属：看板上会显示成
#     "智利本周有 X 条品牌动态"，而那些事根本没发生在智利。
#     区域源的 country_code 留空，除非文章本身点了国名。
#
# ★ 全部经实测（2026-08-12）：只收 HTTP 200、条目 ≥5、且真有消费电子内容的。
#   之前 CO/CL/PE/AR 四国是**空列表**，那四国只能靠品牌检索捡到零星几条 ——
#   实测 CL 只有 77 条、PE 只有 37 条，而 BR/MX 各有 900 条。
#   看板上"智利没什么动静"其实是"我们没去智利看"。
LATAM_FEEDS = {
    "MX": [("Xataka México", "https://www.xataka.com.mx/feedburner.xml", "local"),
           ("Xataka Móvil", "https://www.xatakamovil.com/feedburner.xml", "regional"),
           ("Unocero", "https://www.unocero.com/feed/", "local")],
    "BR": [("Tecnoblog", "https://tecnoblog.net/feed/", "local"),
           ("Canaltech", "https://canaltech.com.br/rss/", "local"),
           ("Olhar Digital", "https://olhardigital.com.br/feed/", "local")],
    "CO": [("ENTER.CO", "https://www.enter.co/feed/", "local"),
           ("El Tiempo Tecnósfera", "https://www.eltiempo.com/rss/tecnosfera.xml", "local")],
    "CL": [("Pisapapeles", "https://www.pisapapeles.net/feed/", "local"),
           ("FayerWayer", "https://www.fayerwayer.com/feed/", "regional")],
    "PE": [("El Comercio Tec",
            "https://elcomercio.pe/arcio/rss/category/tecnologia/", "local")],
    "AR": [("Clarín Tecnología", "https://www.clarin.com/rss/tecnologia/", "local"),
           ("La Nación Tec",
            "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/tecnologia/",
            "local"),
           ("Xataka", "https://www.xataka.com/feedburner.xml", "regional")],
}


def _get(url: str, proxy: str = "", timeout: int = 30) -> str | None:
    try:
        kw = {"timeout": timeout, "follow_redirects": True,
              "headers": {"User-Agent": UA, "Accept": "application/rss+xml,*/*"}}
        if proxy:
            kw["proxy"] = proxy
        r = httpx.get(url, **kw)
        # ★ HTTP 非 200 必须显式失败，不能静默当成"没新闻" ——
        #   否则代理挂了会表现为"友商今天很安静"
        if r.status_code != 200:
            log.warning("RSS HTTP %s: %s", r.status_code, url[:90])
            return None
        return r.text
    except Exception as e:  # noqa: BLE001
        log.warning("RSS 请求失败 %s: %s", url[:90], str(e)[:120])
        return None


def parse_rss(xml: str, source_name: str, limit: int = 60) -> list[dict]:
    if not xml:
        return []
    try:
        soup = BeautifulSoup(xml, "xml")
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(xml, "html.parser")
    out = []
    nodes = soup.find_all("item") or soup.find_all("entry")
    for it in nodes[:limit]:
        title = (it.find("title").get_text(strip=True) if it.find("title") else "")
        link_el = it.find("link")
        link = ""
        if link_el:
            link = link_el.get("href") or link_el.get_text(strip=True)
        pub = ""
        for tag in ("pubDate", "published", "updated", "dc:date"):
            el = it.find(tag)
            if el:
                pub = el.get_text(strip=True)
                break
        desc_el = it.find("description") or it.find("summary") or it.find("content")
        desc = ""
        if desc_el:
            desc = re.sub(r"<[^>]+>", " ", desc_el.get_text(" ", strip=True))[:800]
        if title:
            out.append({"title": title[:300], "url": link, "published_raw": pub,
                        "summary": desc, "source": source_name})
    return out


def search_brand_news(brand_aliases: list[str], category_term: str,
                      country: dict, proxy: str = "", days: int = 7,
                      limit: int = 50) -> list[dict]:
    """按品牌 + 产业词 + 国家检索 Google News"""
    alias_q = " OR ".join(f'"{a}"' for a in brand_aliases[:4])
    q = f"({alias_q}) {category_term} when:{days}d"
    url = GOOGLE_NEWS.format(q=quote(q), hl=country.get("locale", "es-419"),
                             gl=country["code"], lang=country.get("lang", "es"))
    xml = _get(url, proxy)
    if xml is None:
        return []
    return parse_rss(xml, f"GoogleNews/{country['code']}", limit)


def fetch_global_tech(proxy: str = "", limit_per_feed: int = 30) -> list[dict]:
    """扫全球 top 科技站点，用于发现全球新品首发"""
    out = []
    for name, url in GLOBAL_TECH_FEEDS:
        xml = _get(url, proxy)
        if xml:
            out.extend(parse_rss(xml, name, limit_per_feed))
    return out


def fetch_latam_local(country_code: str, proxy: str = "") -> list[dict]:
    """取该国的本地/区域科技媒体。

    每条带 `feed_scope`：上层据此决定要不要把文章标成这个国家。
    区域源的文章不该被记成某一国的动态（见 LATAM_FEEDS 注释）。
    """
    out = []
    for entry in LATAM_FEEDS.get(country_code, []):
        name, url = entry[0], entry[1]
        scope = entry[2] if len(entry) > 2 else "local"
        xml = _get(url, proxy)
        if not xml:
            continue
        for it in parse_rss(xml, name, 30):
            it["feed_scope"] = scope
            out.append(it)
    return out


def fetch_x_posts(bearer_token: str, brand: str, lang: str = "es",
                  limit: int = 20) -> list[dict]:
    """X (Twitter) 官方 API v2。需付费套餐才有搜索权限。
    不填 token 直接返回空 —— 绝不去爬网页版。"""
    if not bearer_token:
        return []
    url = "https://api.twitter.com/2/tweets/search/recent"
    params = {"query": f"{brand} (lanzamiento OR precio OR oferta) lang:{lang} -is:retweet",
              "max_results": min(limit, 100),
              "tweet.fields": "created_at,public_metrics,lang"}
    try:
        r = httpx.get(url, params=params, timeout=30,
                      headers={"Authorization": f"Bearer {bearer_token}"})
        if r.status_code != 200:
            log.warning("X API HTTP %s: %s", r.status_code, r.text[:150])
            return []
        data = r.json().get("data") or []
        return [{"title": (d.get("text") or "")[:300],
                 "url": f"https://twitter.com/i/web/status/{d.get('id')}",
                 "published_raw": d.get("created_at", ""),
                 "summary": "", "source": "X"} for d in data]
    except Exception as e:  # noqa: BLE001
        log.warning("X API 失败: %s", str(e)[:120])
        return []
