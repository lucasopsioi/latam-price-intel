# -*- coding: utf-8 -*-
"""核对每个在抓渠道的 robots.txt 是否允许我们抓**我们真的会去抓的那些 URL**。

★★ 这个文件被重写过一次，原因值得记住：
  第一版只查"有没有**点名**禁止 claudebot/anthropic-ai"。
  这是错的 —— `User-agent: *` 那一段**同样约束我们**。
  代价是我把 Apple 的 App Store 评论 RSS 写进了方案并标成"已实测可行"，
  而 itunes.apple.com/robots.txt 里明明白白写着：

      User-agent: *
      Disallow: /search*        ← 正是我们要用的搜索接口
      Disallow: /*/rss/*        ← 正是我们要用的评论 RSS

  没有点任何名，但把我们要用的两个端点精确地禁掉了。
  ★ 教训：**"没点我的名"不等于"允许我"。** 判定单位是
    「这个 UA 能不能取这个 URL」，不是「有没有出现我的名字」。

★ 用 Python 内置的 urllib.robotparser：它正确处理 `*` 通配、`$` 结尾、
  Allow 优先级、以及"具名段存在时忽略通配段"的规则。
  自己写正则必然漏（第一版就是自己写正则漏的）。

★ 只查不改。发现问题由人决定停用哪个渠道 —— 不自动改 enabled。

用法：
    python tools/check_robots.py            # 查所有启用的渠道
    python tools/check_robots.py --all      # 含已停用的
"""
from __future__ import annotations

import sys
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import db  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 我们这个 agent 可能被匹配到的 UA 串。逐个问，任一被禁就算被禁。
# ★ 通配符 "*" 必须在列表里 —— 漏掉它正是第一版的错误。
OUR_AGENTS = ("*", "ClaudeBot", "Claude-Web", "Claude-User", "anthropic-ai",
              "Anthropic", "Python-urllib", "python-requests")


def probe_urls(ch: dict) -> list[tuple[str, str]]:
    """这个渠道我们**实际会去取**的代表性 URL。空的不查。"""
    out: list[tuple[str, str]] = []
    base = ch.get("base_url") or ""
    if base:
        out.append(("首页/基址", base))
    tpl = ch.get("search_url") or ""
    if tpl:
        # 把占位符换成一个真实感的查询词再判 —— 有的站按路径前缀禁搜索
        out.append(("站内搜索", tpl.replace("{q}", "samsung").replace("{page}", "1")))
    cat = ch.get("category_urls") or ""
    if cat:
        import json
        try:
            m = json.loads(cat) if isinstance(cat, str) else cat
            for k, v in list((m or {}).items())[:2]:
                out.append((f"品类页({k})", v))
        except Exception:  # noqa: BLE001
            pass
    return [(label, u) for label, u in out if u.startswith("http")]


def main() -> int:
    want_all = "--all" in sys.argv
    where = "" if want_all else "WHERE enabled=1"
    rows = db.q(f"SELECT name, country_code, base_url, search_url, category_urls, "
                f"enabled FROM channel {where} ORDER BY country_code, name")
    client = httpx.Client(timeout=20, follow_redirects=True,
                          headers={"User-Agent": UA})
    cache: dict[str, tuple[urllib.robotparser.RobotFileParser | None, str]] = {}

    def parser_for(host: str):
        if host in cache:
            return cache[host]
        rp, note = None, ""
        try:
            resp = client.get(f"https://{host}/robots.txt")
            if resp.status_code == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(resp.text.splitlines())
                # crawl-delay 取所有相关 UA 里最严的
                delays = [d for d in (rp.crawl_delay(a) for a in OUR_AGENTS) if d]
                if delays:
                    note = f"crawl-delay={max(delays)}s"
            else:
                # ★ 取不到 robots.txt **不等于**允许，但也不等于禁止。
                #   如实标出来让人判断，不要默认成"允许"蒙混过去。
                note = f"robots HTTP {resp.status_code}（无规则可依，人工判断）"
        except Exception as e:  # noqa: BLE001
            note = f"robots 取不到（{type(e).__name__}）"
        cache[host] = (rp, note)
        return cache[host]

    print(f"{'国':4}{'渠道':22}{'判定':10}{'受阻的 URL / 说明'}")
    print("-" * 100)
    blocked: list[str] = []
    unknown: list[str] = []
    for r in rows:
        host = urlparse(r["base_url"] or "").netloc
        if not host:
            continue
        rp, note = parser_for(host)
        targets = probe_urls(dict(r))
        if rp is None:
            print(f"{r['country_code']:4}{r['name'][:20]:22}{'未知':10}{note}")
            unknown.append(f"{r['country_code']}/{r['name']}")
            continue

        bad = []
        for label, url in targets:
            # ★ robots.txt 是**按主机**生效的。渠道的搜索页常常在另一个子域
            #   （listado.mercadolibre.com.mx vs www.mercadolibre.com.mx），
            #   拿 base_url 那份规则去判另一个主机的 URL 会判错人 ——
            #   可能冤枉一个好渠道，也可能放过一个真禁的。每个 URL 用自己主机的规则。
            u_host = urlparse(url).netloc or host
            u_rp, u_note = (rp, note) if u_host == host else parser_for(u_host)
            if u_rp is None:
                bad.append(f"{label} 所在主机 {u_host} 取不到 robots（{u_note}）")
                continue
            for agent in OUR_AGENTS:
                if not u_rp.can_fetch(agent, url):
                    bad.append(f"{label}@{u_host} 被 [{agent}] 段禁止 "
                               f"→ {urlparse(url).path[:34] or '/'}")
                    break
        if bad:
            blocked.append(f"{r['country_code']}/{r['name']}")
            print(f"{r['country_code']:4}{r['name'][:20]:22}{'★禁止':10}{bad[0]}")
            for b in bad[1:]:
                print(f"{'':36}{b}")
        else:
            print(f"{r['country_code']:4}{r['name'][:20]:22}{'允许':10}"
                  f"{note}  （查了 {len(targets)} 个 URL）")

    print()
    if blocked:
        print("★★ 以下渠道的 robots.txt **不允许**我们取要取的 URL：")
        for b in blocked:
            print(f"    - {b}")
        print("   处理：停用该渠道，或改走站方官方开放 API。")
        print("   ★ 不要靠改 User-Agent 绕过 —— 那是伪装，本项目不做。")
    else:
        print("所有在抓渠道的目标 URL 均被 robots.txt 允许。")
    if unknown:
        print(f"\n⚠ {len(unknown)} 个渠道取不到 robots.txt，无规则可依，需人工确认：")
        for u in unknown:
            print(f"    - {u}")
    print("\n注：robots.txt 会变，建议每月重跑一次。")
    print("注：本工具判的是「这个 UA 能不能取这个 URL」，"
          "不是「有没有点我的名字」—— 通配段一样算数。")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
